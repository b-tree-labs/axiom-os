# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The program data file — load, validate, save (``axiom.program/0.1``).

One data file per program (prd-program R2): principals, lanes, schedule
items with host-qualified tracker bindings, dates, status, percent. The
site renders from it, an agent updates it, a human can edit it — which is
why this module is strict on the way in (a defect is reported loudly, all
of them at once) and lossless on the way through (a field the loader drops
is worse than a missing field, so unknown fields and unknown top-level
blocks are carried verbatim).

The module is domain-agnostic by construction. Field *names* are the
schema's (``program``, ``lanes``, ``people``, ``schedule``); field *values*
— lane ids, principal names, tracker hosts, binding-block names — are the
deployment's data, and nothing here interprets them beyond their shape.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from . import snapshots

#: The schema identifier a data file must declare.
PROGRAM_SCHEMA = "axiom.program/0.1"

#: Top-level sections the schema enumerates. Every OTHER top-level key is a
#: host-qualified binding block: named by the deployment, shaped by the
#: deployment, carried verbatim by the platform.
KNOWN_SECTIONS = ("schema", "program", "lanes", "people", "schedule", "provenance")

#: The closed status vocabulary (prd-program R1): a date is proposed until
#: its owner commits it. Anything else is free text nobody can report on.
STATUS_VALUES = ("proposed", "committed")

#: ``@name:context`` with a single leading ``@`` (ADR-020); the ``:context``
#: suffix is optional for the home context.
_PRINCIPAL_RE = re.compile(r"^@[\w\-.]+(?::[\w\-.]+)?$")


class ProgramError(ValueError):
    """A program data file could not be read at all."""


class ProgramValidationError(ProgramError):
    """A program data file parsed but does not satisfy the schema.

    Carries ``errors`` — every defect found in one pass, so a human editing
    the file fixes the file once rather than replaying the validator.
    """

    def __init__(self, path: Path | str, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(
            f"{path}: not a valid {PROGRAM_SCHEMA} data file "
            f"({len(errors)} defect{'s' if len(errors) != 1 else ''}):\n  " + "\n  ".join(errors)
        )


@dataclass(frozen=True)
class ProgramData:
    """A validated program data file, held losslessly.

    ``raw`` is the parsed document itself — accessors are views onto it, so
    a round trip writes back exactly what was read plus whatever the caller
    changed, never a re-projection through a narrower shape.
    """

    raw: dict[str, Any]

    @property
    def schema(self) -> str:
        return self.raw["schema"]

    @property
    def program(self) -> dict[str, Any]:
        return self.raw["program"]

    @property
    def lanes(self) -> list[dict[str, Any]]:
        return self.raw.get("lanes", [])

    @property
    def people(self) -> list[dict[str, Any]]:
        return self.raw.get("people", [])

    @property
    def schedule(self) -> list[dict[str, Any]]:
        return self.raw.get("schedule", [])

    @property
    def provenance(self) -> dict[str, Any]:
        return self.raw.get("provenance", {})

    @property
    def bindings(self) -> dict[str, dict[str, Any]]:
        """Top-level blocks the schema does not enumerate — host-qualified
        tracker bindings and whatever else a deployment attaches."""
        return {k: v for k, v in self.raw.items() if k not in KNOWN_SECTIONS}

    # ---- lookups ---------------------------------------------------------

    def lane_ids(self) -> list[str]:
        return [lane["id"] for lane in self.lanes if isinstance(lane, dict) and "id" in lane]

    def person(self, principal: str) -> dict[str, Any] | None:
        for person in self.people:
            if person.get("principal") == principal:
                return person
        return None

    def account(self, principal: str, system: str) -> str | None:
        """A principal's external-account username for ``system`` (e.g.
        ``"gitlab"``), or ``None`` when the person does not declare one.

        Program membership is the principal; the account map is how a feeder
        attributes external activity back to it. An absent person, an absent
        ``accounts`` block, and an explicit ``null`` all read as "no account".
        """
        person = self.person(principal)
        if person is None:
            return None
        accounts = person.get("accounts")
        if not isinstance(accounts, dict):
            return None
        value = accounts.get(system)
        return value if isinstance(value, str) and value else None

    def principal_for_account(self, system: str, username: str | None) -> str | None:
        """The principal whose ``accounts[system]`` is ``username`` — the
        reverse map a feeder uses to attribute a tracker action (an MR by
        ``npl436``) to its principal. ``None`` / empty usernames never match,
        so a person's explicit ``null`` account is not a wildcard."""
        if not isinstance(username, str) or not username:
            return None
        for person in self.people:
            accounts = person.get("accounts")
            if isinstance(accounts, dict) and accounts.get(system) == username:
                principal = person.get("principal")
                if isinstance(principal, str):
                    return principal
        return None

    def deputy(self) -> str | None:
        """The program's deputy principal, when declared (the proxy-assignee
        rule's final fallback)."""
        value = self.program.get("deputy")
        return value if isinstance(value, str) and value else None

    def lane(self, lane_id: str) -> dict[str, Any] | None:
        for lane in self.lanes:
            if isinstance(lane, dict) and lane.get("id") == lane_id:
                return lane
        return None

    def lane_lead(self, lane_id: str) -> str | None:
        """The lane's declared ``lead`` principal, or ``None``. Deployment
        data, carried verbatim; the proxy-assignee rule reads it first."""
        lane = self.lane(lane_id)
        if lane is None:
            return None
        value = lane.get("lead")
        return value if isinstance(value, str) and value else None

    def item(self, item_id: str) -> dict[str, Any] | None:
        for entry in self.schedule:
            if entry.get("id") == item_id:
                return entry
        return None

    def items_for_owner(self, principal: str) -> list[dict[str, Any]]:
        return [e for e in self.schedule if e.get("owner") == principal]

    def items_for_lane(self, lane_id: str) -> list[dict[str, Any]]:
        return [e for e in self.schedule if e.get("lane") == lane_id]

    def tracker(self) -> dict[str, Any]:
        tracker = self.program.get("tracker")
        return tracker if isinstance(tracker, dict) else {}

    def endpoints(self) -> dict[str, str]:
        """The program's declared canonical endpoints (``program.endpoints``).

        A deployment-authored map of a name (``tracker_site``, ``roadmap``,
        ``canonical``, …) to a stable ``http(s)`` URL. These are the stable
        link-out targets a renderer points at, and the stable backlink target a
        later posting phase writes — rather than a volatile per-artifact URL.
        Absent / malformed-shape reads as ``{}`` (the shape is validated on
        load, so a loaded program's endpoints are always well-formed URLs)."""
        raw = self.program.get("endpoints")
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if isinstance(v, str) and v}


# ---- validation -----------------------------------------------------------


def _check_date(value: Any, where: str, errors: list[str]) -> None:
    if not isinstance(value, str):
        errors.append(f"{where}: date must be an ISO string, got {value!r}")
        return
    try:
        date.fromisoformat(value)
    except ValueError:
        errors.append(f"{where}: {value!r} is not an ISO date (YYYY-MM-DD)")


def _check_principal(value: Any, where: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not _PRINCIPAL_RE.match(value):
        errors.append(f"{where}: {value!r} is not a principal of the form @name or @name:context")


def validate_program(raw: Any, *, require_listed_owners: bool = True) -> list[str]:
    """Validate a parsed document against ``axiom.program/0.1``.

    Returns every defect found, in document order, in one pass. An empty
    list means the document is valid.

    ``require_listed_owners=False`` skips exactly one rule — an item's owner
    must be listed in ``people`` — and nothing else. It exists for the
    ``drift`` read, whose job is to *report* that inconsistency rather than
    refuse the file over it; every other reader keeps the default.
    """
    if not isinstance(raw, dict):
        return [f"top level must be an object, got {type(raw).__name__}"]

    errors: list[str] = []

    declared = raw.get("schema")
    if declared != PROGRAM_SCHEMA:
        errors.append(f"schema: expected {PROGRAM_SCHEMA!r}, got {declared!r}")

    # -- program ------------------------------------------------------------
    program = raw.get("program")
    if not isinstance(program, dict):
        errors.append("program: required object is missing")
        program = {}
    for field in ("id", "name"):
        value = program.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"program.{field}: required non-empty string is missing")
    tracker = program.get("tracker")
    if tracker is not None:
        if not isinstance(tracker, dict):
            errors.append("program.tracker: must be an object when present")
        elif not isinstance(tracker.get("host"), str) or not tracker["host"].strip():
            errors.append("program.tracker.host: a tracker binding must name its host")
    # endpoints: the optional canonical-endpoint map. Each value is a stable
    # http(s) URL a renderer links out to and a backlink targets; the names are
    # the deployment's data (not a closed set), carried verbatim.
    endpoints = program.get("endpoints")
    if endpoints is not None:
        if not isinstance(endpoints, dict):
            errors.append("program.endpoints: must be an object of name → URL when present")
        else:
            for name, url in endpoints.items():
                if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                    errors.append(
                        f"program.endpoints.{name}: must be an http(s) URL, got {url!r}"
                    )

    # -- lanes ----------------------------------------------------------------
    lanes = raw.get("lanes", [])
    lane_ids: list[str] = []
    if not isinstance(lanes, list):
        errors.append("lanes: must be a list")
        lanes = []
    for i, lane in enumerate(lanes):
        where = f"lanes[{i}]"
        if not isinstance(lane, dict):
            errors.append(f"{where}: must be an object")
            continue
        lane_id = lane.get("id")
        if not isinstance(lane_id, str) or not lane_id.strip():
            errors.append(f"{where}.id: required non-empty string is missing")
            continue
        if lane_id in lane_ids:
            errors.append(f"{where}.id: duplicate lane id {lane_id!r}")
        lane_ids.append(lane_id)

    # -- people ---------------------------------------------------------------
    people = raw.get("people", [])
    principals: list[str] = []
    if not isinstance(people, list):
        errors.append("people: must be a list")
        people = []
    for i, person in enumerate(people):
        where = f"people[{i}]"
        if not isinstance(person, dict):
            errors.append(f"{where}: must be an object")
            continue
        principal = person.get("principal")
        _check_principal(principal, f"{where}.principal", errors)
        if isinstance(principal, str):
            if principal in principals:
                errors.append(f"{where}.principal: duplicate principal {principal!r}")
            principals.append(principal)
        person_lanes = person.get("lanes", [])
        if not isinstance(person_lanes, list):
            errors.append(f"{where}.lanes: must be a list of lane ids")
        else:
            for lane_id in person_lanes:
                if lane_id not in lane_ids:
                    errors.append(f"{where}.lanes: unknown lane {lane_id!r}")
        # accounts: the optional external-account map. The systems are the
        # deployment's data (not a closed set), so the keys are uninterpreted;
        # each value must be a string username or an explicit null ("this
        # person has no account on that system").
        accounts = person.get("accounts")
        if accounts is not None:
            if not isinstance(accounts, dict):
                errors.append(f"{where}.accounts: must be an object of system → username|null")
            else:
                for system, username in accounts.items():
                    if username is not None and not isinstance(username, str):
                        errors.append(
                            f"{where}.accounts.{system}: must be a string username or null, "
                            f"got {username!r}"
                        )

    # -- schedule ---------------------------------------------------------------
    schedule = raw.get("schedule", [])
    item_ids: list[str] = []
    if not isinstance(schedule, list):
        errors.append("schedule: must be a list")
        schedule = []
    for i, entry in enumerate(schedule):
        where = f"schedule[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be an object")
            continue
        item_id = entry.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            errors.append(f"{where}.id: required non-empty string is missing")
        else:
            where = f"schedule[{i}] ({item_id})"
            if item_id in item_ids:
                errors.append(f"{where}: duplicate item id {item_id!r}")
            item_ids.append(item_id)
        if not isinstance(entry.get("label"), str) or not entry["label"].strip():
            errors.append(f"{where}.label: required non-empty string is missing")
        lane_ref = entry.get("lane")
        if lane_ref is not None and lane_ref not in lane_ids:
            errors.append(f"{where}.lane: unknown lane {lane_ref!r}")
        owner = entry.get("owner")
        if owner is not None:
            _check_principal(owner, f"{where}.owner", errors)
            if require_listed_owners and isinstance(owner, str) and owner not in principals:
                errors.append(
                    f"{where}.owner: {owner!r} is not listed in people — an item's "
                    "owner must be a principal the ownership map knows"
                )
        status = entry.get("status")
        if status is not None and status not in STATUS_VALUES:
            errors.append(f"{where}.status: {status!r} is not one of " + " | ".join(STATUS_VALUES))
        pct = entry.get("pct")
        if pct is not None and (
            not isinstance(pct, int) or isinstance(pct, bool) or not 0 <= pct <= 100
        ):
            errors.append(f"{where}.pct: must be an integer 0..100, got {pct!r}")
        has_point = "date" in entry
        has_span = "start" in entry or "end" in entry
        if has_point:
            _check_date(entry["date"], f"{where}.date", errors)
        if has_span:
            for field in ("start", "end"):
                if field not in entry:
                    errors.append(f"{where}.{field}: a span needs both start and end")
                else:
                    _check_date(entry[field], f"{where}.{field}", errors)
        if not has_point and not has_span:
            errors.append(f"{where}: needs a date, or a start and end pair")

    # -- binding blocks -----------------------------------------------------
    for key, value in raw.items():
        if key not in KNOWN_SECTIONS and not isinstance(value, dict):
            errors.append(f"{key}: a binding block must be an object, got {type(value).__name__}")

    return errors


# ---- load / save ------------------------------------------------------------


def load_program(path: str | Path, *, require_listed_owners: bool = True) -> ProgramData:
    """Read and validate a program data file.

    ``require_listed_owners`` is passed to :func:`validate_program`; only the
    ``drift`` read turns it off.

    Raises:
        ProgramError: the file is missing or is not JSON.
        ProgramValidationError: the file parsed but violates the schema;
            ``.errors`` names every defect found.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProgramError(f"cannot read program data file {path}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProgramError(f"{path} is not valid JSON: {exc}") from exc
    errors = validate_program(raw, require_listed_owners=require_listed_owners)
    if errors:
        raise ProgramValidationError(path, errors)
    return ProgramData(raw=raw)


def _content_differs(path: Path, raw: dict[str, Any]) -> bool:
    """Whether writing ``raw`` to ``path`` is a real change.

    A missing or unreadable/corrupt current file counts as a change — writing
    over it is exactly the recovery a backup exists for. A byte-formatting-only
    difference (e.g. indentation) is NOT a change: the comparison is on the
    parsed documents, so a re-save of identical content is a no-op backup-wise.
    """
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return existing != raw


def save_program(
    data: ProgramData,
    path: str | Path,
    *,
    snapshot: bool = True,
    now: Any = None,
    policy: Any = None,
    snapshots_dir: str | Path | None = None,
) -> Path:
    """Write a program data file, validating on the way out.

    A process that can write an invalid file leaves the next reader holding
    the failure, so saving re-validates and refuses the same way loading
    does.

    The write is **atomic**: the new content lands in a temp file in the same
    directory, is flushed to disk, and is then ``os.replace``-d over the target
    in one step. A crash, a kill, or a second writer therefore never leaves a
    torn, half-written ``data.json`` that every later reader would refuse — the
    file is only ever the complete old content or the complete new content.
    This is the authoritative editable source of truth (CLERK rewrites it every
    reconcile), so a truncating write is the one failure mode that would brick
    the program read; the pattern here mirrors ``axiom.infra.state``'s
    ``LockedJsonFile.write``.

    **Rolling backup (defense-in-depth).** When the write is a real *change*
    (not a no-op re-save), a timestamped copy of the payload is written into a
    separate ``snapshots/`` history beside the file and the history is pruned
    to a bounded retention policy (:mod:`.snapshots`). The atomic replace above
    prevents a torn file at write time; this history lets the program be rolled
    back to a known-good prior state after any corruption, bad write, or
    unwanted autonomous change. It is additive and never touches the
    change-detection ``snapshot.json``. A backup/prune failure is swallowed —
    the authoritative write has already succeeded and must not fail over its
    own safety net. Pass ``snapshot=False`` to skip it; ``now`` / ``policy`` /
    ``snapshots_dir`` are test seams.
    """
    errors = validate_program(data.raw)
    if errors:
        raise ProgramValidationError(path, errors)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data.raw, indent=1, ensure_ascii=False) + "\n"

    changed = _content_differs(path, data.raw)

    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    if snapshot and changed and snapshots.snapshots_enabled():
        try:
            snapshots.write_snapshot(
                snapshots_dir
                if snapshots_dir is not None
                else snapshots.snapshots_dir_for(path),
                payload,
                now=now,
                policy=policy,
            )
        except OSError:
            # The rolling backup is a safety net, not the write itself — a
            # failure here must never turn a successful authoritative write
            # into a raised error for the caller.
            pass
    return path
