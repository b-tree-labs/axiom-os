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
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

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


def validate_program(raw: Any) -> list[str]:
    """Validate a parsed document against ``axiom.program/0.1``.

    Returns every defect found, in document order, in one pass. An empty
    list means the document is valid.
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
            if isinstance(owner, str) and owner not in principals:
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


def load_program(path: str | Path) -> ProgramData:
    """Read and validate a program data file.

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
    errors = validate_program(raw)
    if errors:
        raise ProgramValidationError(path, errors)
    return ProgramData(raw=raw)


def save_program(data: ProgramData, path: str | Path) -> Path:
    """Write a program data file, validating on the way out.

    A process that can write an invalid file leaves the next reader holding
    the failure, so saving re-validates and refuses the same way loading
    does.
    """
    errors = validate_program(data.raw)
    if errors:
        raise ProgramValidationError(path, errors)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data.raw, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
