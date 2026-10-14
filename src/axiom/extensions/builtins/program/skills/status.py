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

The ``drift`` scope (prd-program R11, phase 2) reports what the data file
by itself shows to be out of line — items with no owner, owners missing
from ``people``, items with no tracker binding, lanes with no items — each
as an explicit finding. It does not consult the external tracker (that
needs capture, a later phase), and its result says so: ``basis`` is
``"data-file-only"`` and ``not_checked`` names what was not compared.

Every refusal carries ``value["refused"]`` — ``absent`` (the file does not
know the key), ``bad_request`` (the question is malformed) or ``no_data``
(the file is missing or invalid) — so a transport maps it without parsing
the error text.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..model import ProgramData, ProgramError, load_program
from . import _capture as cap
from ._source import ABSENT, BAD_REQUEST, NO_DATA, resolve_data_path, resolve_endpoints

SCOPES = ("person", "lane", "item", "schedule", "priorities", "drift")
FORMATS = ("brief", "full")

#: Scopes whose answer is relative to one key.
_KEYED_SCOPES = ("person", "lane", "item")

#: Item fields the brief view already presents structurally; everything
#: else in an entry surfaces only under ``fmt=full``. ``tracker`` is here
#: because its content is surfaced structurally (``_tracker_content``), so
#: ``fmt=full`` must not re-dump the raw feeder block on top of it.
_CORE_FIELDS = ("id", "label", "owner", "start", "end", "date", "status", "pct", "tracker")


#: The drift checks, in the order findings are reported per subject. Each
#: is decidable from the data file alone.
DRIFT_CHECKS = (
    "unowned_item",
    "owner_not_in_people",
    "unlaned_item",
    "unbound_item",
    "issue_without_tracker",
    "empty_lane",
)

#: What the drift scope's *data-file-only* findings do NOT compare, stated in
#: every drift result so "no findings" is never read as "in sync". Live link
#: and mirror health are reported separately under the ``crosslink`` facet
#: (what a feeder last verified); these remain uncompared by either.
DRIFT_NOT_CHECKED = (
    "the external tracker's live item state beyond link resolution (assignees, "
    "task-list membership) — the crosslink facet reports what a feeder verified",
    "roll-up membership across clerk-to-clerk composition — a later phase",
)


def _refuse(kind: str, message: str) -> SkillResult:
    return SkillResult(ok=False, value={"refused": kind}, errors=[message])


def _links(entry: dict[str, Any], data: ProgramData) -> list[dict[str, Any]]:
    """Every link the data file states for an entry.

    Two shapes, both straight from the file: a string field that is already
    a URL, and a tracker issue made host-qualified by the program's declared
    tracker binding. No URL grammar is invented for a tracker the platform
    does not know — the host, project and ref are handed over as data.

    Each link carries an **access hint**: ``access`` names the system that
    opening it needs (``gitlab`` / ``github`` / the tracker's declared kind for
    a tracker link; ``None`` for a plain URL), and ``gated`` is ``True`` when
    opening it needs that system's access. A gated link is "edit / see more if
    you have access" — the item's substance is surfaced separately (see
    ``_tracker_content``), so a viewer without the tracker still sees it.
    """
    links: list[dict[str, Any]] = []
    for field, value in entry.items():
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            links.append({"kind": "url", "field": field, "href": value, "access": None, "gated": False})
    issue = entry.get("issue")
    tracker = data.tracker()
    if issue is not None and tracker.get("host"):
        access = tracker.get("kind") or "tracker"
        links.append(
            {
                "kind": "tracker",
                "host": tracker["host"],
                "project": tracker.get("project_id"),
                "ref": issue,
                "access": access,
                "gated": True,
            }
        )
    return links


def _tracker_content(entry: dict[str, Any]) -> dict[str, Any] | None:
    """The item's substance as captured from the tracker, so a viewer without
    the tracker's access still sees it — title, state, assignee, due, labels,
    last activity. Written by a feeder into ``entry["tracker"]``; **absent is
    absent**: no feeder content → ``None``, never an invented title/state.
    """
    tracker = entry.get("tracker")
    if not isinstance(tracker, dict):
        return None
    last_activity = None
    activity = entry.get("activity")
    if isinstance(activity, list) and activity:
        last_activity = activity[-1]
    return {
        "title": tracker.get("title"),
        "state": tracker.get("state"),
        "assignee": tracker.get("assignee"),
        "due": tracker.get("due"),
        "milestone": tracker.get("milestone"),
        "labels": tracker.get("labels"),
        "updated_at": tracker.get("updated_at"),
        "last_activity": last_activity,
    }


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
        # The item's substance from the tracker (account-aware reads): a viewer
        # without GitLab/GitHub access sees it here; None when no feeder has
        # populated it (absent is absent).
        "tracker": _tracker_content(entry),
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


def _drift_findings(data: ProgramData) -> list[dict[str, Any]]:
    """Every data-file-detectable inconsistency, as explicit findings.

    Order: schedule items in file order (each item's checks in
    :data:`DRIFT_CHECKS` order), then lanes in file order.
    """
    principals = {p.get("principal") for p in data.people}
    has_tracker = bool(data.tracker().get("host"))
    findings: list[dict[str, Any]] = []

    def add(kind: str, subject: str, detail: str) -> None:
        findings.append({"kind": kind, "subject": subject, "detail": detail})

    for entry in data.schedule:
        item_id = entry.get("id")
        owner = entry.get("owner")
        if owner is None:
            add("unowned_item", item_id, "schedule item names no owner")
        elif owner not in principals:
            add(
                "owner_not_in_people",
                item_id,
                f"owner {owner!r} is not listed in the program's people",
            )
        if entry.get("lane") is None:
            add("unlaned_item", item_id, "schedule item belongs to no lane")
        if entry.get("issue") is None:
            add("unbound_item", item_id, "schedule item has no tracker issue binding")
        elif not has_tracker:
            add(
                "issue_without_tracker",
                item_id,
                f"issue {entry['issue']!r} cannot resolve: the program declares no tracker",
            )
    laned = {e.get("lane") for e in data.schedule}
    for lane_id in data.lane_ids():
        if lane_id not in laned:
            add("empty_lane", lane_id, "lane has no schedule items")
    return findings


#: The capture-recorded finding kinds the drift read surfaces under its
#: ``crosslink`` facet: link health + repository-mirror health.
_CROSSLINK_KINDS = frozenset(cap.LINK_FINDING_KINDS + cap.MIRROR_FINDING_KINDS)


def _crosslink_facet(data: ProgramData) -> dict[str, Any]:
    """The live-checked half of drift (R11 link + mirror health), surfaced on
    the drift read from what a feeder last recorded in the ``capture`` block.

    The data-file-only findings above are decidable with no network; this facet
    is what a live feeder's crosslink / mirror pass found — dead links, mirror
    gaps — carried on the same drift surface so any renderer sees them.
    Detection only: a ``dead_link`` carries its ``proposed_fix`` (the correct
    backlink target) for the later posting phase, never applied here. When no
    feeder has run, link health is ``unchecked`` — never silently "healthy".
    """
    capture = data.raw.get("capture")
    if not isinstance(capture, dict):
        return {
            "basis": "unchecked",
            "checked": False,
            "findings": [],
            "note": (
                "no feeder has run a crosslink check; run `program sync` with a "
                "live source (--source-kind gitlab|github) to verify links"
            ),
        }
    findings = [
        f
        for f in capture.get("findings", []) or []
        if isinstance(f, dict) and f.get("kind") in _CROSSLINK_KINDS
    ]
    return {
        "basis": "capture",
        "checked": True,
        "source": capture.get("source"),
        "link_health": capture.get("link_health"),
        "findings": findings,
        "count": len(findings),
    }


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    scope = params.get("scope")
    if scope not in SCOPES:
        return _refuse(BAD_REQUEST, f"unknown scope {scope!r}; supported: " + ", ".join(SCOPES))
    fmt = params.get("fmt", "brief")
    if fmt not in FORMATS:
        return _refuse(BAD_REQUEST, f"unknown fmt {fmt!r}; supported: " + ", ".join(FORMATS))
    key = params.get("key")
    if scope in _KEYED_SCOPES and not key:
        return _refuse(BAD_REQUEST, f"scope {scope!r} requires a key")

    data_path, refusal = resolve_data_path(params, ctx)
    if refusal is not None:
        return _refuse(BAD_REQUEST, refusal)
    try:
        # drift reads a file whose owners are missing from people — that is
        # one of the things it reports; every other defect still refuses.
        data = load_program(data_path, require_listed_owners=scope != "drift")
    except ProgramError as exc:
        return _refuse(NO_DATA, str(exc))

    value: dict[str, Any] = {
        "scope": scope,
        "key": key,
        "fmt": fmt,
        "program": {
            "id": data.program.get("id"),
            "name": data.program.get("name"),
            "as_of": data.program.get("as_of"),
        },
        # The program's canonical endpoints, resolved so any renderer can link
        # out to a stable target (and a backlink can target it) rather than a
        # volatile per-artifact URL.
        "endpoints": resolve_endpoints(data, ctx),
    }

    if scope == "drift":
        findings = _drift_findings(data)
        value["basis"] = "data-file-only"
        value["checked"] = list(DRIFT_CHECKS)
        value["not_checked"] = list(DRIFT_NOT_CHECKED)
        value["findings"] = findings
        value["count"] = len(findings)
        # The live-checked half (link + mirror health) from the last feeder run.
        value["crosslink"] = _crosslink_facet(data)
        return SkillResult(ok=True, value=value)

    if scope == "person":
        person = data.person(key)
        if person is None:
            return _refuse(ABSENT, f"no principal {key!r} in the program's people")
        entries = data.items_for_owner(key)
        if fmt == "full":
            value["person"] = person
    elif scope == "lane":
        if key not in data.lane_ids():
            return _refuse(ABSENT, f"no lane {key!r} in the program")
        entries = data.items_for_lane(key)
    elif scope == "item":
        entry = data.item(key)
        if entry is None:
            return _refuse(ABSENT, f"no item {key!r} in the schedule")
        entries = [entry]
    elif scope == "priorities":
        entries = _priorities(data)
    else:  # schedule
        entries = list(data.schedule)

    value["items"] = [_item_view(e, data, fmt) for e in entries]
    value["count"] = len(value["items"])
    return SkillResult(ok=True, value=value)
