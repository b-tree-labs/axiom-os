# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Program skills — invocable through the platform SkillRegistry (ADR-056).

Each skill is a plain function ``(params, ctx) -> SkillResult``, namespaced
under the extension's CLI noun: ``program.status``, ``program.render``,
``program.validate``. The CLI verbs are 1:1 thin wrappers; any agent or
harness reaches the identical surface through the registry.

Phase 1 of prd-program shipped the read tool (R1 partial), the renderer,
and the data-file validator over the one-file-per-program contract (R2).
Phase 2 projects the two reads — ``status`` and ``validate`` — onto the
composed MCP through this registry (ADR-073: ``surfaces`` opts in,
``side_effects=False`` makes them read-only tools) and adds the
data-file-only ``drift`` scope (R11). ``render`` writes files and stays on
the CLI.

Phase 3 adds the self-update core: ``sync`` reconciles the data file against
its source and appends the change log (a write, CLI + the scheduled clerk's
heartbeat, never an MCP tool), and ``changes`` answers "what changed since I
last looked" against a per-consumer watermark (a read tool — ``side_effects
=False`` — whose watermark advance is an explicit, opt-in write; see
``changes.py``). The remaining R1 verbs (collect, draft, post, priorities,
attach) arrive with the coordinator phases.
"""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from . import (
    changes,
    invitation,
    items,
    lanes,
    ownership,
    people,
    render,
    status,
    sync,
    validate,
)

_NAMESPACE = "program"

#: The reads: CLI, the composed MCP, and agent tools (ADR-073).
_READ = ("cli", "mcp", "agent_tool")
#: Writes on the node — never projected past the operator's shell (and the
#: scheduled clerk reaches ``sync`` through the ``cli`` heartbeat command).
#:
#: The mutation verbs (person/lane/item/invite/redeem) are CLI-only for the
#: same reason ``sync`` is: they write the node's authoritative state and must
#: not be reachable as an anonymous MCP tool. The in-body deputy/maintainer
#: gate is the data-driven authority check (ADR-114 is a transport gate and
#: cannot read the data file's deputy); ``surfaces=("cli",)`` is the structural
#: floor. A deployment that wants a mutation over MCP opts it in deliberately
#: by adding ``"mcp"`` here AND declaring ``allowed_principals`` — never
#: anonymous. See spec-program §"Read vs. write exposure".
_CLI_ONLY = ("cli",)

#: verb → (function, description, inputs, side_effects, surfaces)
_SKILLS = {
    "status": (
        status.run,
        "One parameterized read over the program data file: items with "
        "owner, dates, proposed-or-committed status, percent, and links, "
        "scoped by person, lane, item, schedule, or priorities; or the "
        "drift scope — what the data file alone shows is inconsistent "
        "(unowned items, owners not in people, items with no tracker "
        "binding, empty lanes), labelled data-file-only. Answers only from "
        "the data file — absent is reported as absent, never invented.",
        {"scope": "str!", "key": "str", "fmt": "str", "data": "Path"},
        False,
        _READ,
    ),
    "render": (
        render.run,
        "Render the program data file to one static HTML status page in an "
        "output directory. Built-in template, stdlib only; every item id "
        "and every link in the file is present on the page.",
        {"data": "Path", "out": "Path"},
        True,
        _CLI_ONLY,
    ),
    "validate": (
        validate.run,
        "Check a program data file against the axiom.program/0.1 schema "
        "and report every defect at once, or the file's shape when it is "
        "valid.",
        {"data": "Path"},
        False,
        _READ,
    ),
    "sync": (
        sync.run,
        "Reconcile the program data file against its source(s) and append one "
        "change-log entry per difference. Idempotent: a second run over an "
        "unchanged source logs nothing. source_kind selects the feeder (file "
        "= self-reconcile, gitlab, github, or all); a live feeder that cannot "
        "pass the connector-readiness ladder is skipped loudly, never treated "
        "as 'no changes'. Updates data.json only when a distinct source "
        "changed; reads only read-only sources and writes only the node's own "
        "state. CLI + the scheduled clerk's heartbeat; never an MCP tool.",
        {"data": "Path", "source": "Path", "source_kind": "str"},
        True,
        _CLI_ONLY,
    ),
    "changes": (
        changes.run,
        "What changed since this principal last looked: the change-log "
        "entries after the caller's watermark, with the same owner/dates/"
        "status/percent/links shape as status. Reporting is a read; "
        "advancing the watermark is a write that is the default only on the "
        "operator's CLI and otherwise requires an explicit advance — so the "
        "projected tool stays read-only. --since last (default) or an ISO "
        "timestamp; --peek reports without advancing.",
        {"principal": "str", "since": "str", "peek": "bool", "advance": "bool"},
        False,
        _READ,
    ),
    "ownership": (
        ownership.run,
        "The ownership of an item or lane over time: the current owner from "
        "the data file plus the timeline reconstructed from the change log — "
        "each span a principal with from/to timestamps and who set it. A past "
        "owner is reported accurately even after they leave the roster, "
        "because the timeline is the immutable log, not the current people[]. "
        "Read-only.",
        {"scope": "str!", "key": "str", "data": "Path"},
        False,
        _READ,
    ),
    # ---- the mutation surface (CLI-only; identity-gated in-body) ----------
    "person_add": (
        people.add,
        "Add a program member: a principal (@name:context) layered with a "
        "lane and optional role and accounts. Resolved through the directory "
        "seam when a provider is configured, else accepted as given. "
        "Deputy/maintainer-gated; appends a person_added change-log entry.",
        {"principal": "str!", "lane": "list[str]", "role": "str", "name": "str", "account": "list[str]", "drives": "str", "data": "Path"},
        True,
        _CLI_ONLY,
    ),
    "person_edit": (
        people.edit,
        "Edit a member's name, drives, or accounts (lane/role moves go through "
        "person reassign). Deputy/maintainer-gated; appends person_edited.",
        {"principal": "str!", "name": "str", "drives": "str", "account": "list[str]", "data": "Path"},
        True,
        _CLI_ONLY,
    ),
    "person_remove": (
        people.remove,
        "Remove a member. Refuses if they still own schedule items unless "
        "--reassign-to names another member to take them. Deputy/maintainer-"
        "gated; appends person_removed (and owner_changed for any reassigned "
        "items).",
        {"principal": "str!", "reassign_to": "str", "data": "Path"},
        True,
        _CLI_ONLY,
    ),
    "person_reassign": (
        people.reassign,
        "Move a member to a different set of lanes and/or change their program "
        "role. Deputy/maintainer-gated; appends person_reassigned with old→new.",
        {"principal": "str!", "lane": "list[str]", "role": "str", "data": "Path"},
        True,
        _CLI_ONLY,
    ),
    "lane_add": (
        lanes.add,
        "Add a lane (id, name, optional lead, optional color). Deputy/"
        "maintainer-gated; appends lane_added.",
        {"id": "str!", "name": "str", "lead": "str", "color": "str", "data": "Path"},
        True,
        _CLI_ONLY,
    ),
    "lane_edit": (
        lanes.edit,
        "Edit a lane's name/color, or change its lead. A lead change is a "
        "first-class ownership transition (lane_owner_changed, old→new); a "
        "name/color change appends lane_edited. Deputy/maintainer-gated.",
        {"id": "str!", "name": "str", "lead": "str", "color": "str", "data": "Path"},
        True,
        _CLI_ONLY,
    ),
    "lane_remove": (
        lanes.remove,
        "Remove a lane. Refuses if it still owns schedule items unless "
        "--reassign-to names another lane to take them. Deputy/maintainer-"
        "gated; appends lane_removed.",
        {"id": "str!", "reassign_to": "str", "data": "Path"},
        True,
        _CLI_ONLY,
    ),
    "item_add": (
        items.add,
        "Add a schedule item (id, label, optional owner/lane/dates/status/pct/"
        "issue). An owner with no tracker account gets the ADR-166 proxy "
        "assignment recorded, never posted. Deputy/maintainer-gated; appends "
        "item_added.",
        {"id": "str!", "label": "str!", "owner": "str", "lane": "str", "date": "str", "start": "str", "end": "str", "status": "str", "pct": "int", "issue": "str", "data": "Path"},
        True,
        _CLI_ONLY,
    ),
    "item_edit": (
        items.edit,
        "Edit an item's label/lane/dates/status/pct/issue (owner changes go "
        "through item reassign). Deputy/maintainer-gated; logs the matching "
        "field change-kind (date_changed, status_changed, …) or item_edited "
        "for a label.",
        {"id": "str!", "label": "str", "lane": "str", "date": "str", "start": "str", "end": "str", "status": "str", "pct": "int", "issue": "str", "data": "Path"},
        True,
        _CLI_ONLY,
    ),
    "item_remove": (
        items.remove,
        "Remove a schedule item. Deputy/maintainer-gated; appends item_removed.",
        {"id": "str!", "data": "Path"},
        True,
        _CLI_ONLY,
    ),
    "item_reassign": (
        items.reassign,
        "Change an item's owner, recomputing the ADR-166 proxy assignment when "
        "the new owner has no account on the program's tracker system — "
        "recorded on item.assignment, never posted to any tracker. Deputy/"
        "maintainer-gated; logs owner_changed with old→new and who acted.",
        {"id": "str!", "owner": "str!", "data": "Path"},
        True,
        _CLI_ONLY,
    ),
    "invite": (
        invitation.invite,
        "Issue a single-use, expiring invitation to join the program at a lane "
        "and role, riding the platform gate invitation primitive (shrink-only "
        "scope, scrypt-hashed at rest). The code redeems once; the key is "
        "minted on the invitee's side. Deputy/maintainer-gated; appends "
        "invited.",
        {"principal": "str!", "lane": "str!", "role": "str", "name": "str", "account": "list[str]", "expires": "str", "invitations_file": "Path", "data": "Path"},
        True,
        _CLI_ONLY,
    ),
    "redeem": (
        invitation.redeem,
        "Redeem an invitation code and record the program membership (honoring "
        "the missing-account onboarding finding). The code is the "
        "authentication, so this is not deputy-gated. Appends redeemed and "
        "person_added.",
        {"code": "str!", "invitations_file": "Path", "data": "Path"},
        True,
        _CLI_ONLY,
    ),
}


def bind(registry: SkillRegistry) -> None:
    """Register every program skill into ``registry``, with its spec."""
    for verb, (fn, description, inputs, side_effects, surfaces) in _SKILLS.items():
        name = f"{_NAMESPACE}.{verb}"
        if registry.has(name):
            continue
        registry.register_skill(
            SkillSpec(
                name=name,
                fn=fn,
                description=description,
                inputs=inputs,
                side_effects=side_effects,
                idempotent=True,
                surfaces=surfaces,
            )
        )


def bind_default() -> SkillRegistry:
    """Bind into the process-local default registry; idempotent."""
    registry = default_registry()
    bind(registry)
    return registry


def verbs() -> list[str]:
    """Verb names without the namespace prefix. Used by the CLI parser."""
    return list(_SKILLS)


__all__ = ["bind", "bind_default", "verbs"]
