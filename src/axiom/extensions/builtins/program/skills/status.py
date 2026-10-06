# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.status`` — one parameterized read over the program data file.

The read tool of prd-program R1: items with owner, dates, proposed-or-
committed state, percent, and links, scoped by person, lane, item, the
whole schedule, or the stated priorities.

The skill answers ONLY from the data file. A field the file does not carry
comes back as ``None``; a key the file does not know comes back as a
refusal naming the key. Nothing is inferred, estimated, or invented —
status reporting earns trust by refusing to fill gaps.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..model import ProgramData, ProgramError, load_program

SCOPES = ("person", "lane", "item", "schedule", "priorities")
FORMATS = ("brief", "full")

#: Scopes whose answer is relative to one key.
_KEYED_SCOPES = ("person", "lane", "item")

#: Item fields the brief view already presents structurally; everything
#: else in an entry surfaces only under ``fmt=full``.
_CORE_FIELDS = ("id", "label", "owner", "start", "end", "date", "status", "pct")


def _default_data_path(ctx: SkillContext) -> Path:
    return ctx.state_dir / "program" / "data.json"


def _links(entry: dict[str, Any], data: ProgramData) -> list[dict[str, Any]]:
    """Every link the data file states for an entry.

    Two shapes, both straight from the file: a string field that is already
    a URL, and a tracker issue made host-qualified by the program's declared
    tracker binding. No URL grammar is invented for a tracker the platform
    does not know — the host, project and ref are handed over as data.
    """
    links: list[dict[str, Any]] = []
    for field, value in entry.items():
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            links.append({"kind": "url", "field": field, "href": value})
    issue = entry.get("issue")
    tracker = data.tracker()
    if issue is not None and tracker.get("host"):
        links.append(
            {
                "kind": "tracker",
                "host": tracker["host"],
                "project": tracker.get("project_id"),
                "ref": issue,
            }
        )
    return links


def _item_view(entry: dict[str, Any], data: ProgramData, fmt: str) -> dict[str, Any]:
    view: dict[str, Any] = {
        "id": entry.get("id"),
        "label": entry.get("label"),
        "owner": entry.get("owner"),
        "dates": {
            "start": entry.get("start"),
            "end": entry.get("end"),
            "date": entry.get("date"),
        },
        "status": entry.get("status"),
        "pct": entry.get("pct"),
        "links": _links(entry, data),
    }
    if fmt == "full":
        for field, value in entry.items():
            if field not in _CORE_FIELDS:
                view[field] = value
    return view


def _priorities(data: ProgramData) -> list[dict[str, Any]]:
    """The status-bearing items, committed before proposed, earliest due
    first. An entry with no status is not a priority anybody stated, so it
    is left out rather than promoted."""

    def sort_key(entry: dict[str, Any]) -> tuple[int, str]:
        rank = 0 if entry.get("status") == "committed" else 1
        due = entry.get("end") or entry.get("date") or "9999-12-31"
        return (rank, due)

    stated = [e for e in data.schedule if e.get("status") is not None]
    return sorted(stated, key=sort_key)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    scope = params.get("scope")
    if scope not in SCOPES:
        return SkillResult(
            ok=False,
            errors=[f"unknown scope {scope!r}; supported: " + ", ".join(SCOPES)],
        )
    fmt = params.get("fmt", "brief")
    if fmt not in FORMATS:
        return SkillResult(
            ok=False,
            errors=[f"unknown fmt {fmt!r}; supported: " + ", ".join(FORMATS)],
        )
    key = params.get("key")
    if scope in _KEYED_SCOPES and not key:
        return SkillResult(ok=False, errors=[f"scope {scope!r} requires a key"])

    data_path = Path(params.get("data") or _default_data_path(ctx))
    try:
        data = load_program(data_path)
    except ProgramError as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    value: dict[str, Any] = {
        "scope": scope,
        "key": key,
        "fmt": fmt,
        "program": {
            "id": data.program.get("id"),
            "name": data.program.get("name"),
            "as_of": data.program.get("as_of"),
        },
    }

    if scope == "person":
        person = data.person(key)
        if person is None:
            return SkillResult(ok=False, errors=[f"no principal {key!r} in the program's people"])
        entries = data.items_for_owner(key)
        if fmt == "full":
            value["person"] = person
    elif scope == "lane":
        if key not in data.lane_ids():
            return SkillResult(ok=False, errors=[f"no lane {key!r} in the program"])
        entries = data.items_for_lane(key)
    elif scope == "item":
        entry = data.item(key)
        if entry is None:
            return SkillResult(ok=False, errors=[f"no item {key!r} in the schedule"])
        entries = [entry]
    elif scope == "priorities":
        entries = _priorities(data)
    else:  # schedule
        entries = list(data.schedule)

    value["items"] = [_item_view(e, data, fmt) for e in entries]
    value["count"] = len(value["items"])
    return SkillResult(ok=True, value=value)
