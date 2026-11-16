# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``actors.list`` / ``actors.show``: Team on every surface (MVP-23, ADR-056).

Both read; neither stores anything (MVP-23-A4). The viewer's class decides which
fields leave the server (MVP-23-A5): the HTTP route passes the signed-in person's
roles as ``_viewer_roles``. A caller with no roles named is the operator at the
node's own terminal or stdio session when no request is in flight, and a member
when one is, so a surface that forgets to say who is asking shows less, never
more.
"""

from __future__ import annotations

from typing import Any

from axiom.governance.roles import canonical_role, display_name
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec

from .. import directory, visibility

KINDS = ("person", "agent", "node")


def _viewer(params: dict[str, Any]) -> str:
    roles = params.get("_viewer_roles")
    if roles is not None:
        return visibility.viewer_class(roles)
    from axiom.infra.site_authority import caller_principal

    return visibility.MEMBER if caller_principal() else visibility.ADMIN


def _as_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [p.strip() for p in raw.split(",") if p.strip()]
    return [str(p).strip() for p in raw if str(p).strip()]


def list_actors(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    del ctx
    kinds = _as_list(params.get("kind") or params.get("kinds"))
    unknown = [k for k in kinds if k not in KINDS]
    if unknown:
        return SkillResult(ok=False, errors=[f"unknown kind(s) {unknown}; choose from {list(KINDS)}"])
    role = canonical_role(params.get("role"))
    viewer = _viewer(params)
    rows, skipped = directory.build_rows(agents=params.get("_agents"))
    if kinds:
        rows = [r for r in rows if r["kind"] in kinds]
    if role:
        rows = [r for r in rows if role in (r.get("roles") or [])]
    findings = [{"handle": r["handle"], "finding": r["finding"]} for r in rows if r.get("finding")]
    counts = {k: sum(1 for r in rows if r["kind"] == k) for k in KINDS}
    roles = sorted({x for r in rows if r["kind"] == "person" for x in r.get("roles") or ()})
    value: dict[str, Any] = {
        "actors": [visibility.project(r, viewer) for r in rows],
        "counts": counts,
        "roles": roles,
        # What a person reads for each role id ("Site admin", "Access admin"; ADR-261).
        "role_names": {r: display_name(r) for r in roles},
        "findings": findings,
        "viewer": viewer,
        "fields": visibility.visible_fields(viewer),
        "skipped": skipped,
        "can": {"invite": viewer == visibility.ADMIN, "set_roles": viewer == visibility.ADMIN,
                "suspend": viewer == visibility.ADMIN},
    }
    if viewer == visibility.ADMIN:
        from axiom.extensions.builtins.webgate.role_bundles import default_bundle_registry

        value["roles_defined"] = sorted(default_bundle_registry().roles())
        value["role_names"].update({r: display_name(r) for r in value["roles_defined"]})
    notes = [f"{counts['person']} people, {counts['agent']} agents, {counts['node']} nodes"]
    notes += [f"{f['handle']}: {f['finding']}" for f in findings]
    return SkillResult(ok=True, value=value, actions_taken=notes)


def show_actor(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    del ctx
    handle = str(params.get("handle") or "").strip()
    if not handle:
        return SkillResult(ok=False, errors=["name the actor: handle is required (@name:context)"])
    row = directory.find(handle, agents=params.get("_agents"))
    if row is None:
        # The same answer for "not here" and "not yours to see" (MVP-10-A1).
        return SkillResult(ok=False, errors=[f"no actor {handle!r} here"])
    return SkillResult(ok=True, value=visibility.project(row, _viewer(params)))


SPECS = (
    SkillSpec(
        name="actors.list",
        fn=list_actors,
        description=(
            "Team: the people, agents and nodes that can act here, in one list, each naming "
            "its kind; filter by kind and role. Agents name their accountable person, and one "
            "without is a finding. Fields follow the asker's role. Reads only."
        ),
        inputs={"kind": "person | agent | node (comma list)", "role": "only actors holding this role"},
        surfaces=("cli", "mcp", "agent_tool"),
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
    ),
    SkillSpec(
        name="actors.show",
        fn=show_actor,
        description=(
            "One actor by handle (@name:context): kind, roles, contact channels, and for an agent "
            "its accountable person. An actor the asker may not see answers like one that does "
            "not exist. Reads only."
        ),
        inputs={"handle": "the actor's @name:context"},
        surfaces=("cli", "mcp", "agent_tool"),
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
    ),
)


def bind(registry: SkillRegistry) -> None:
    for spec in SPECS:
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=False)


def bind_default() -> SkillRegistry:
    registry = SkillRegistry()
    bind(registry)
    return registry


__all__ = ["SPECS", "bind", "bind_default", "list_actors", "show_actor"]
