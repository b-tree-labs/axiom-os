# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The change log, the snapshot, and the diff between them (phase 3).

``data.json`` is the program's CURRENT state; this module is the HISTORY
beside it. Three files live in ``<state_dir>/program/``:

- ``data.json`` — the current state (owned by :mod:`..model`).
- ``snapshot.json`` — what ``sync`` last reconciled. The diff memory: a
  content hash per item and per field, so re-running ``sync`` over an
  unchanged source produces no diff and logs nothing. This is the drift
  resilience — a change seen twice is not logged twice.
- ``changelog.jsonl`` — the append-only history. One line per change, each
  a stable ``kind`` from :data:`CHANGE_KINDS`, the subject and field, the
  ``old``→``new`` values, the ``source`` origin, a monotonic ``seq`` (its
  1-based position, which per-consumer watermarks key on), and a ``ts``.

Every write goes through ``axiom.infra.state`` — ``locked_append_jsonl`` for
the log, ``LockedJsonFile`` for the snapshot and watermarks — never a bare
``open()``: the CLI, the scheduled clerk, and a served read can all touch
these at once.

The module is domain-agnostic: it compares the *shape* of a program data
file (item ids, lane ids, the schema's field names) and never interprets a
value beyond equality.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext
from axiom.infra.state import LockedJsonFile, locked_append_jsonl

from ..model import ProgramData
from .status import _drift_findings

#: The snapshot's own schema id — versioned apart from the data file so the
#: diff memory can evolve without touching ``axiom.program/0.1``.
SNAPSHOT_SCHEMA = "axiom.program.snapshot/0.1"

#: The closed change-kind vocabulary. Every change-log entry's ``kind`` is
#: one of these; nothing else is ever written. A stable vocabulary is what
#: lets a consumer filter ("show me owner changes") and a renderer map a
#: kind to chrome without parsing prose.
#:
#: The first twelve are produced by the snapshot diff (``sync`` + any edit
#: that touches a tracked item/lane/drift field). The rest are produced by
#: the mutation surface (``program person|lane|item|invite|redeem``) for the
#: facts the snapshot does not track — people, lane leads, lane names, item
#: labels, and invitations. Extending this tuple is a deliberate edit
#: (ADR-165): add a kind only when a mutation genuinely needs one, and
#: document it in spec-program §"The change log".
CHANGE_KINDS = (
    "item_added",
    "item_removed",
    "owner_changed",
    "date_changed",
    "status_changed",
    "pct_changed",
    "issue_linked",
    "issue_cleared",
    "lane_added",
    "lane_removed",
    "drift_opened",
    "drift_cleared",
    # --- the mutation surface (program.{person,lane,item,invite,redeem}) ---
    "item_edited",          # a non-tracked item field changed (label, custom)
    "lane_edited",          # a lane's name/color changed
    "lane_owner_changed",   # a lane's lead changed — old→new, provenance in `by`
    "person_added",         # a principal joined people[]
    "person_removed",       # a principal left people[]
    "person_edited",        # a person's name/accounts/other fields changed
    "person_reassigned",    # a person's lanes and/or program role changed
    "invited",              # an invitation to join was issued
    "redeemed",             # an invitation was redeemed into a membership
    "dead_link_opened",
    "dead_link_cleared",
)

#: Drift finding kinds that log under their own open/clear change kinds rather
#: than the generic ``drift_opened`` / ``drift_cleared`` — crosslink health, so
#: a consumer can filter "a link went dead" distinctly from any other drift.
_DRIFT_KIND_CHANGES = {
    "dead_link": ("dead_link_opened", "dead_link_cleared"),
}

#: The item fields the snapshot tracks for per-field diffing. ``lane`` and
#: the three date fields are here too; which *kind* each change becomes is
#: decided in :func:`_diff_item`.
_ITEM_FIELDS = ("owner", "status", "pct", "issue", "lane", "date", "start", "end")

#: The subject-kind tag on a change record, so ``lane_added`` on an item
#: (an item joined a lane) is never confused with ``lane_added`` on the
#: program (a lane was defined). Also lets a consumer group by subject.
SUBJECT_ITEM = "item"
SUBJECT_LANE = "lane"
SUBJECT_PROGRAM = "program"
SUBJECT_DRIFT = "drift"
#: A change about a program member (``person_*`` kinds). The subject is the
#: member's ``@name:context`` principal.
SUBJECT_PERSON = "person"
#: A change about an invitation (``invited`` / ``redeemed``). The subject is
#: the invitation id.
SUBJECT_INVITATION = "invitation"


# ---- paths ----------------------------------------------------------------


def program_state_dir(ctx: SkillContext) -> Path:
    return ctx.state_dir / "program"


def changelog_path(ctx: SkillContext) -> Path:
    return program_state_dir(ctx) / "changelog.jsonl"


def snapshot_path(ctx: SkillContext) -> Path:
    return program_state_dir(ctx) / "snapshot.json"


def watermarks_path(ctx: SkillContext) -> Path:
    return program_state_dir(ctx) / "watermarks.json"


# ---- content hashing ------------------------------------------------------


def _hash(value: Any) -> str:
    """A stable content hash over a JSON-able value (sorted keys)."""
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---- snapshot -------------------------------------------------------------


def _item_fields(entry: dict[str, Any]) -> dict[str, Any]:
    """The tracked fields of one schedule item, each present (``None`` when
    the file does not state it) so a diff can tell a cleared field from an
    untracked one."""
    return {field: entry.get(field) for field in _ITEM_FIELDS}


def snapshot_of(data: ProgramData) -> dict[str, Any]:
    """Build the diff snapshot of a program.

    Per item: the tracked field values plus a ``hash`` over them, so an
    unchanged item is skipped in O(1) and the per-item hash requirement is
    met literally. Plus the program's lane-id set and its data-file drift
    findings, so lane and drift transitions are diffable too.
    """
    items: dict[str, Any] = {}
    for entry in data.schedule:
        item_id = entry.get("id")
        if not isinstance(item_id, str):
            continue
        fields = _item_fields(entry)
        items[item_id] = {**fields, "hash": _hash(fields)}

    lane_ids = data.lane_ids()
    # Drift is the union of the data-file-only findings (status.py) and the
    # capture-half findings a feeder recorded in the ``capture`` block
    # (account_missing, mirror_gap, …). Both diff as drift_opened/
    # drift_cleared, so a feeder's detection reaches ``changes`` with no extra
    # plumbing. A data file with no ``capture`` block is unchanged.
    drift = [{"kind": f["kind"], "subject": f["subject"]} for f in _drift_findings(data)]
    capture = data.raw.get("capture")
    if isinstance(capture, dict):
        for finding in capture.get("findings", []) or []:
            if isinstance(finding, dict) and finding.get("kind"):
                drift.append({"kind": finding["kind"], "subject": finding.get("subject")})

    return {
        "schema": SNAPSHOT_SCHEMA,
        "program_id": data.program.get("id"),
        "lanes": lane_ids,
        "items": items,
        "drift": drift,
    }


# ---- diff -----------------------------------------------------------------


def _change(
    kind: str,
    subject_kind: str,
    subject: Any,
    *,
    field: str | None = None,
    old: Any = None,
    new: Any = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "subject_kind": subject_kind,
        "subject": subject,
        "field": field,
        "old": old,
        "new": new,
    }


def _diff_item(item_id: str, old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-field changes for one item present on both sides. Order is a
    fixed, stable field order so a reader sees changes the same way twice."""
    changes: list[dict[str, Any]] = []

    if old.get("owner") != new.get("owner"):
        changes.append(
            _change(
                "owner_changed",
                SUBJECT_ITEM,
                item_id,
                field="owner",
                old=old.get("owner"),
                new=new.get("owner"),
            )
        )
    if old.get("status") != new.get("status"):
        changes.append(
            _change(
                "status_changed",
                SUBJECT_ITEM,
                item_id,
                field="status",
                old=old.get("status"),
                new=new.get("status"),
            )
        )
    if old.get("pct") != new.get("pct"):
        changes.append(
            _change(
                "pct_changed",
                SUBJECT_ITEM,
                item_id,
                field="pct",
                old=old.get("pct"),
                new=new.get("pct"),
            )
        )

    old_issue, new_issue = old.get("issue"), new.get("issue")
    if old_issue != new_issue:
        if old_issue is None:
            changes.append(
                _change(
                    "issue_linked", SUBJECT_ITEM, item_id, field="issue", old=None, new=new_issue
                )
            )
        elif new_issue is None:
            changes.append(
                _change(
                    "issue_cleared", SUBJECT_ITEM, item_id, field="issue", old=old_issue, new=None
                )
            )
        else:
            # Relinked to a different ref: the old binding is cleared and a
            # new one linked, reported as a linked change carrying both.
            changes.append(
                _change(
                    "issue_linked",
                    SUBJECT_ITEM,
                    item_id,
                    field="issue",
                    old=old_issue,
                    new=new_issue,
                )
            )

    old_lane, new_lane = old.get("lane"), new.get("lane")
    if old_lane != new_lane:
        if old_lane is not None:
            changes.append(
                _change("lane_removed", SUBJECT_ITEM, item_id, field="lane", old=old_lane, new=None)
            )
        if new_lane is not None:
            changes.append(
                _change("lane_added", SUBJECT_ITEM, item_id, field="lane", old=None, new=new_lane)
            )

    for field in ("date", "start", "end"):
        if old.get(field) != new.get(field):
            changes.append(
                _change(
                    "date_changed",
                    SUBJECT_ITEM,
                    item_id,
                    field=field,
                    old=old.get(field),
                    new=new.get(field),
                )
            )

    return changes


def diff_snapshots(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    """Every change from ``old`` to ``new`` snapshot, as raw change records
    (``seq``/``ts``/``source`` are stamped by the caller).

    An empty ``old`` (the first sync, no prior snapshot) is a baseline:
    every current item, lane and drift finding is reported as added/opened,
    which is exactly right for a consumer whose watermark starts at zero —
    everything is new to someone who has looked at nothing.

    Deterministic order: item changes in new-file item order, then removed
    items in old order, then lanes, then drift.
    """
    old_items = old.get("items", {}) or {}
    new_items = new.get("items", {}) or {}
    changes: list[dict[str, Any]] = []

    for item_id, new_fields in new_items.items():
        old_fields = old_items.get(item_id)
        if old_fields is None:
            # Just "added" — the item's fields travel with the current item
            # view the changes read enriches each delta with, so the log stays
            # one line per item rather than a flurry of field changes.
            changes.append(_change("item_added", SUBJECT_ITEM, item_id))
        elif old_fields.get("hash") != new_fields.get("hash"):
            changes.extend(_diff_item(item_id, old_fields, new_fields))

    for item_id in old_items:
        if item_id not in new_items:
            changes.append(_change("item_removed", SUBJECT_ITEM, item_id))

    old_lanes = list(old.get("lanes", []) or [])
    new_lanes = list(new.get("lanes", []) or [])
    for lane_id in new_lanes:
        if lane_id not in old_lanes:
            changes.append(_change("lane_added", SUBJECT_LANE, lane_id))
    for lane_id in old_lanes:
        if lane_id not in new_lanes:
            changes.append(_change("lane_removed", SUBJECT_LANE, lane_id))

    def _drift_key(finding: dict[str, Any]) -> tuple[Any, Any]:
        return (finding.get("kind"), finding.get("subject"))

    old_drift = {_drift_key(f) for f in old.get("drift", []) or []}
    new_drift_list = new.get("drift", []) or []
    new_drift = {_drift_key(f) for f in new_drift_list}
    for finding in new_drift_list:
        if _drift_key(finding) not in old_drift:
            opened, _ = _DRIFT_KIND_CHANGES.get(finding.get("kind"), ("drift_opened", None))
            changes.append(
                _change(opened, SUBJECT_DRIFT, finding.get("subject"), field=finding.get("kind"))
            )
    for finding in old.get("drift", []) or []:
        if _drift_key(finding) not in new_drift:
            _, cleared = _DRIFT_KIND_CHANGES.get(finding.get("kind"), (None, "drift_cleared"))
            changes.append(
                _change(cleared, SUBJECT_DRIFT, finding.get("subject"), field=finding.get("kind"))
            )

    return changes


# ---- storage --------------------------------------------------------------


def load_snapshot(path: str | Path) -> dict[str, Any]:
    """The last snapshot, or ``{}`` when there is none yet."""
    with LockedJsonFile(path) as f:
        data = f.read()
    return data if isinstance(data, dict) else {}


def save_snapshot(path: str | Path, snapshot: dict[str, Any]) -> None:
    with LockedJsonFile(path, exclusive=True) as f:
        f.write(snapshot)


def read_changelog(path: str | Path) -> list[dict[str, Any]]:
    """Every change-log entry in order, or ``[]`` when the log is absent.

    Read under a shared lock on the same ``.lock`` file
    ``locked_append_jsonl`` takes exclusively, so a read never races a
    half-written append. A line that does not parse is skipped rather than
    failing the whole read — the log is advisory history, not the state.
    """
    path = Path(path)
    if not path.exists():
        return []
    import os
    import sys

    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if sys.platform != "win32":
            try:
                import fcntl

                fcntl.flock(lock_fd, fcntl.LOCK_SH)
            except (ImportError, OSError):
                pass
        text = path.read_text(encoding="utf-8")
    finally:
        if sys.platform != "win32":
            try:
                import fcntl

                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
        os.close(lock_fd)

    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            entries.append(record)
    return entries


def append_change(path: str | Path, record: dict[str, Any]) -> None:
    """Append one change record to the log (multi-process-safe)."""
    locked_append_jsonl(path, record)


# ---- watermarks -----------------------------------------------------------


def read_watermark(path: str | Path, principal: str) -> dict[str, Any] | None:
    """A principal's last-reported position, or ``None`` if it never looked."""
    with LockedJsonFile(path) as f:
        marks = f.read()
    if not isinstance(marks, dict):
        return None
    mark = marks.get(principal)
    return mark if isinstance(mark, dict) else None


def advance_watermark(path: str | Path, principal: str, *, seq: int, ts: str, at: str) -> None:
    """Record that ``principal`` has now seen the log up to ``seq`` / ``ts``.

    Read-modify-write under an exclusive lock, so two principals advancing
    at once never clobber each other's mark. Never moves a mark backwards —
    a stale caller re-reporting an old window cannot un-see newer entries.
    """
    with LockedJsonFile(path, exclusive=True) as f:
        marks = f.read()
        if not isinstance(marks, dict):
            marks = {}
        prior = marks.get(principal) if isinstance(marks.get(principal), dict) else {}
        prior_seq = prior.get("seq", 0) if isinstance(prior, dict) else 0
        if not isinstance(prior_seq, int):
            prior_seq = 0
        if seq < prior_seq:
            seq = prior_seq
            ts = prior.get("ts", ts)
        marks[principal] = {"seq": seq, "ts": ts, "updated_at": at}
        f.write(marks)


__all__ = [
    "SNAPSHOT_SCHEMA",
    "CHANGE_KINDS",
    "SUBJECT_ITEM",
    "SUBJECT_LANE",
    "SUBJECT_PROGRAM",
    "SUBJECT_DRIFT",
    "SUBJECT_PERSON",
    "SUBJECT_INVITATION",
    "program_state_dir",
    "changelog_path",
    "snapshot_path",
    "watermarks_path",
    "snapshot_of",
    "diff_snapshots",
    "load_snapshot",
    "save_snapshot",
    "read_changelog",
    "append_change",
    "read_watermark",
    "advance_watermark",
]
