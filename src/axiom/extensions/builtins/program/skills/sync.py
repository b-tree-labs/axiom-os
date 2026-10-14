# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.sync`` — reconcile the data file against its source(s), log the diff.

The self-update step (prd-program R3/R4, R11). One pass per source:

1. Read the current state from a *source* (:mod:`.sources`). The default
   source is the node's own ``data.json`` (self-reconcile); an explicit
   ``source`` file, or a live ``gitlab`` / ``github`` feeder selected by
   ``source_kind``, stands in as an upstream.
2. A live source first climbs the connector-readiness ladder (R9); one that
   cannot be verified is **skipped loudly** — recorded in ``skipped`` with the
   rung it failed — never treated as "no changes".
3. Update ``data.json`` when a distinct source's content differs.
4. Diff the source against the last snapshot and append one change-log entry
   per difference, then advance the snapshot.

Reconciliation is **idempotent**: the snapshot is a content hash per item and
per field, so running ``sync`` twice over an unchanged source logs nothing
the second time. It assumes it is not the only writer — each reconcile runs
under an exclusive lock on the snapshot, so concurrent syncs serialize rather
than double-log.

``source_kind`` selects the feeder (``file`` | ``gitlab`` | ``github`` |
``all``). With no selection it reads ``program.feeders`` from the data file,
falling back to self-reconcile so CLERK's heartbeat stays a safe local no-op
until a deployment opts a live feeder in. This is the action the CLERK agent
runs on its heartbeat; posting back to a tracker is a later phase.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult
from axiom.infra.state import LockedJsonFile

from ..model import ProgramData, ProgramError, save_program
from . import _changelog as cl
from ._source import BAD_REQUEST, NO_DATA, resolve_data_path
from .sources import SOURCE_KINDS, FileSource, ProgramSource, build_source


def _refuse(kind: str, message: str) -> SkillResult:
    return SkillResult(ok=False, value={"refused": kind}, errors=[message])


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _declared_feeders(target: Path) -> list[str]:
    """The live feeders the data file declares in ``program.feeders``.

    Read straight from JSON (no validation) so peeking at the config never
    turns a drift-only file into a refusal. Only known live kinds survive.
    """
    try:
        doc = json.loads(Path(target).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    feeders = (doc.get("program") or {}).get("feeders")
    if not isinstance(feeders, list):
        return []
    return [f for f in feeders if f in SOURCE_KINDS and f not in ("file", "all")]


def _inferred_feeders(target: Path) -> list[str]:
    """When ``source=all`` and nothing is declared, infer feeders from the
    tracker host and whether mirror pairs are declared."""
    try:
        doc = json.loads(Path(target).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    program = doc.get("program") or {}
    kinds: list[str] = []
    tracker_kind = (program.get("tracker") or {}).get("kind")
    if tracker_kind in ("gitlab", "github"):
        kinds.append(tracker_kind)
    if program.get("mirrors") and "github" not in kinds:
        kinds.append("github")
    return kinds


def _resolve_sources(
    params: dict[str, Any], ctx: SkillContext, target: Path
) -> tuple[list[ProgramSource] | None, str | None]:
    """The source(s) this sync reconciles from, or a refusal message."""
    injected = params.get("_sources")
    if injected is not None:
        return list(injected), None

    source_param = params.get("source")
    if source_param:
        # An explicit upstream file must be strictly valid — it may be copied
        # into data.json, which every reader must accept.
        return [FileSource(source_param, require_listed_owners=True)], None

    kind = params.get("source_kind")
    if kind is not None and kind not in SOURCE_KINDS:
        return None, f"unknown source_kind {kind!r}; one of {', '.join(SOURCE_KINDS)}"

    if kind in ("gitlab", "github"):
        return [build_source(kind, target, state_dir=ctx.state_dir)], None

    if kind == "all":
        feeders = _declared_feeders(target) or _inferred_feeders(target)
        if feeders:
            return [build_source(k, target, state_dir=ctx.state_dir) for k in feeders], None
        # nothing live configured → a safe self-reconcile
        return [FileSource(target, require_listed_owners=False)], None

    # kind is None or "file": the configured feeders, else self-reconcile.
    if kind is None:
        feeders = _declared_feeders(target)
        if feeders:
            return [build_source(k, target, state_dir=ctx.state_dir) for k in feeders], None
    return [FileSource(target, require_listed_owners=False)], None


def _reconcile_one(
    source: ProgramSource,
    target: Path,
    ctx: SkillContext,
    ts: str,
) -> dict[str, Any]:
    """One source's reconcile pass. Returns a per-source summary; never raises
    for a connector problem — an unverified or unreadable source is reported,
    not crashed through."""
    summary: dict[str, Any] = {
        "source": source.origin,
        "verified": True,
        "updated": False,
        "recorded": [],
        "data": None,
        "skipped": None,
    }

    readiness = source.verify()
    if not readiness.verified:
        summary["verified"] = False
        summary["skipped"] = {
            "source": source.origin,
            "reason": readiness.detail or "connector unverified",
            "failed_rung": readiness.failed_rung,
        }
        return summary

    # A ProgramError here (invalid source data) propagates: ``run`` turns it
    # into a typed ``no_data`` refusal for a single source — an invalid
    # upstream must never overwrite good state — and a loud skip under
    # ``source=all``. Being *unverified* is the skip case (handled above);
    # producing *invalid data* is the refuse case.
    source_data = source.load()

    if source_data is None:
        summary["note"] = f"source {source.origin} produced no program data; nothing to reconcile"
        return summary

    summary["data"] = source_data

    # Write-back when a *distinct* source's content differs (the self-reconcile
    # source IS the file, so nothing is rewritten).
    if source.origin != f"file:{target}":
        current = None
        if Path(target).exists():
            try:
                current = json.loads(Path(target).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                current = None
        if current != source_data.raw:
            save_program(source_data, target)
            summary["updated"] = True

    # Diff + append under one exclusive lock on the snapshot.
    recorded: list[dict[str, Any]] = []
    with LockedJsonFile(cl.snapshot_path(ctx), exclusive=True) as snap:
        old_snapshot = snap.read()
        if not isinstance(old_snapshot, dict):
            old_snapshot = {}
        new_snapshot = cl.snapshot_of(source_data)
        raw_changes = cl.diff_snapshots(old_snapshot, new_snapshot)
        changelog = cl.changelog_path(ctx)
        base_seq = len(cl.read_changelog(changelog))
        for offset, change in enumerate(raw_changes, start=1):
            record = {"seq": base_seq + offset, "ts": ts, "source": source.origin, **change}
            cl.append_change(changelog, record)
            recorded.append(record)
        snap.write(new_snapshot)

    summary["recorded"] = recorded
    summary["snapshot"] = new_snapshot
    return summary


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    target, refusal = resolve_data_path(params, ctx)
    if refusal is not None:
        return _refuse(BAD_REQUEST, refusal)
    assert target is not None

    sources, src_refusal = _resolve_sources(params, ctx, target)
    if src_refusal is not None:
        return _refuse(BAD_REQUEST, src_refusal)
    assert sources is not None

    ts = _now_iso()
    all_changes: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    per_source: list[dict[str, Any]] = []
    data_updated = False
    last_data: ProgramData | None = None
    last_snapshot: dict[str, Any] | None = None
    notes: list[str] = []

    for source in sources:
        try:
            summary = _reconcile_one(source, target, ctx, ts)
        except ProgramError as exc:
            # The single explicit-upstream/self-reconcile path keeps its typed
            # no_data refusal (an invalid upstream must not overwrite state).
            if len(sources) == 1:
                return _refuse(NO_DATA, str(exc))
            skipped.append({"source": source.origin, "reason": str(exc), "failed_rung": None})
            continue

        per_source.append(
            {
                "source": summary["source"],
                "verified": summary["verified"],
                "updated": summary["updated"],
                "count": len(summary["recorded"]),
                "skipped": summary["skipped"] is not None,
            }
        )
        if summary["skipped"] is not None:
            skipped.append(summary["skipped"])
            continue
        if summary.get("note"):
            notes.append(summary["note"])
        all_changes.extend(summary["recorded"])
        data_updated = data_updated or summary["updated"]
        if summary["data"] is not None:
            last_data = summary["data"]
        if summary.get("snapshot") is not None:
            last_snapshot = summary["snapshot"]

    # A single source that produced nothing (missing file) keeps the phase-3
    # "nothing to reconcile" shape so the verb is always a safe no-op.
    if last_data is None and not skipped and len(sources) == 1:
        return SkillResult(
            ok=True,
            value={
                "source": sources[0].origin,
                "data_updated": False,
                "changes": [],
                "count": 0,
                "skipped": [],
                "note": notes[0] if notes else "nothing to reconcile",
            },
        )

    program = last_data.program if last_data is not None else {}
    value: dict[str, Any] = {
        "program": (
            {"id": program.get("id"), "name": program.get("name"), "as_of": program.get("as_of")}
            if last_data is not None
            else None
        ),
        "source": sources[0].origin if len(sources) == 1 else (params.get("source_kind") or "all"),
        "data_updated": data_updated,
        "changes": all_changes,
        "count": len(all_changes),
        "skipped": skipped,
        "sources": per_source,
    }
    if last_snapshot is not None:
        value["snapshot"] = {
            "items": len(last_snapshot.get("items", {})),
            "lanes": len(last_snapshot.get("lanes", [])),
            "drift": len(last_snapshot.get("drift", [])),
        }
    if notes:
        value["note"] = "; ".join(notes)

    actions = [f"appended {len(all_changes)} change(s)"] if all_changes else []
    if skipped:
        actions.append(f"skipped {len(skipped)} unverified source(s)")
    return SkillResult(ok=True, value=value, actions_taken=actions)
