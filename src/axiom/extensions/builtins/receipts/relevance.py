# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Which brief items a viewer is SHOWN: the relevance gate (DC-17).

Owner, 2026-10-31, looking at a site's chat landing that listed another
machine's heartbeat, a month-old backup and "not yet watched here" under the
input: "I hate seeing things I can do nothing about. Show information only if
it has high value for the user persona's role(s) or for taking imminent action.
Otherwise keep it tucked out of the way for quick search or nav-guided
discovery." And the clarification that came with it: "Don't go too crazy
chopping things out." **Prominence is personal; reachability is uniform.**

So this module decides prominence only. It never removes anything from the
brief: ``/api/v1/receipts/today`` (Control, Log, Site > System, the CLI and the
MCP courier) still carries every case, trust change and decided case. What it
composes is the short list a landing or a "Needs you" chip may PUSH to one
viewer, and an item is on it only when

1. it is **about this site's own node(s)**: never another machine's telemetry
   (a developer laptop that once reported to the site); and
2. it is **role-relevant** to the viewer (node health belongs to the roles
   that govern node operations: owner, admin, operator) **or** it carries an
   **action the viewer may run now and that works** (a declared fix that
   applies here, and a credential that may post the decision).

Node-health claims for one node are ONE item ("node-a health: 3 stale
reports"), never a card per claim; the case construct already groups them.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

#: The claim kinds that are node operations. Their audience is the roles that
#: run the node, never everyone who can open the site.
OPS_CLAIM_KINDS = frozenset(
    {"heartbeat", "backup", "backup_validate", "service_health", "canary", "versions", "agents"}
)

#: The scope that marks a role as owning node operations (ADR-151: owner ``*``,
#: admin ``*:govern``, operator ``*:govern``; reader, contributor, creator and
#: viewer do not govern).
OPS_SCOPE = "fleet:govern"

#: The scope a decision POST needs (``/receipts/case/<id>/decide``). A button
#: the gate would refuse is not an action this viewer can take.
DECIDE_SCOPE = "receipts:invoke"

#: Further node ids a multi-node site runs on, comma separated. The node this
#: process runs on is always included; nothing else is unless declared here.
SITE_NODES_ENV = "AXIOM_SITE_NODES"


@dataclass(frozen=True)
class Viewer:
    """Who is looking, as the gate resolved them. Never from the request body."""

    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()

    def covers(self, wanted: str) -> bool:
        from axiom.extensions.builtins.webgate.role_bundles import _covers

        return any(_covers(g, wanted) for g in self.scopes)

    @property
    def owns_node_operations(self) -> bool:
        return self.covers(OPS_SCOPE)

    @property
    def may_decide(self) -> bool:
        return self.covers(DECIDE_SCOPE)


#: A node with no gate in front of it (a developer's own machine): the person
#: at the keyboard runs the node.
LOCAL_OWNER = Viewer(roles=("owner",), scopes=("*",))


def viewer_from_roles(roles: Iterable[str], scopes: Iterable[str] = ()) -> Viewer:
    """A viewer from gate roles (resolved through the role bundles) plus any
    scopes an issued key carries."""
    from axiom.extensions.builtins.webgate.role_bundles import default_bundle_registry

    roles = tuple(str(r) for r in roles if r)
    granted = set(str(s) for s in scopes if s)
    granted.update(default_bundle_registry().resolve(roles))
    return Viewer(roles=roles, scopes=tuple(sorted(granted)))


def viewer_from_request(request) -> Viewer:
    """The signed-in viewer: a session's roles, else a bearer key's scopes.

    No gate configured at all is the local owner. A gate that resolves nobody
    is an empty viewer, who is pushed nothing (and can still search and
    navigate to everything their credential reads).
    """
    from axiom.extensions.builtins.fleet import api as fleet_api

    resolver = fleet_api._session_resolver_factory()
    credential = resolver(request) if resolver is not None else None
    if credential is not None:
        claims = getattr(credential, "claims", None) or {}
        roles = claims.get("roles") or ()
        if isinstance(roles, str):
            roles = [roles]
        return viewer_from_roles(roles, getattr(credential, "scopes", ()) or ())
    identity = fleet_api._bearer_identity(request)
    if identity is not None:
        return viewer_from_roles(
            getattr(identity, "roles", ()) or (), getattr(identity, "scopes", ()) or ()
        )
    return LOCAL_OWNER if resolver is None else Viewer()


def site_nodes(this_node: str) -> frozenset[str]:
    """The node(s) this site runs on: this one, plus any declared."""
    extra = {n.strip() for n in os.environ.get(SITE_NODES_ENV, "").split(",") if n.strip()}
    return frozenset({this_node, *extra} - {""})


def _is_ops(case: dict) -> bool:
    items = case.get("items") or []
    return (
        case.get("entity_kind") == "node"
        and bool(items)
        and all(i.get("claim_kind") in OPS_CLAIM_KINDS for i in items)
    )


def _working_action(case: dict, viewer: Viewer) -> dict | None:
    """The one-click action, only where it would run and the viewer may run it."""
    handling = case.get("handling") or {}
    fix = next((o for o in case.get("options") or [] if o.get("choice") == "fix"), None)
    if not (fix and handling.get("can_run") and viewer.may_decide):
        return None
    return {
        "label": handling.get("fix_summary") or fix.get("label") or "Fix",
        "choice": "fix",
        "case_id": case["case_id"],
        "runs": fix.get("runs") or handling.get("runs") or "",
    }


def _health_title(case: dict) -> str:
    from axiom.infra.text_utils import pluralize

    items = case.get("items") or []
    statuses = {i.get("status") for i in items}
    word = next(iter(statuses)) if len(statuses) == 1 else "out-of-date or failing"
    return f"{case['entity_id']} health: {pluralize(len(items), f'{word} report')}"


def attention_items(brief: dict, viewer: Viewer, *, this_node: str) -> list[dict]:
    """The brief's cases this viewer is pushed, in the brief's order.

    ``brief`` is :func:`brief_payload`'s shape. Nothing is removed from it.
    """
    ours = site_nodes(this_node)
    out: list[dict] = []
    for case in brief.get("cases") or []:
        if case.get("entity_kind") == "node" and case.get("entity_id") not in ours:
            continue  # another machine's telemetry: Site > System, never the landing
        ops = _is_ops(case)
        relevant = viewer.owns_node_operations if ops else False
        action = _working_action(case, viewer) if (relevant or not ops) else None
        if not (relevant or action):
            continue
        handling = case.get("handling") or {}
        out.append(
            {
                "id": case["case_id"],
                "kind": "node_health" if ops else "case",
                "title": _health_title(case) if ops else case.get("title") or "",
                "detail": handling.get("because") or "",
                "severity": case.get("severity") or "",
                "action": action,
                # Where the whole case lives, for everyone who can read it.
                "href": f"#/control?case={case['case_id']}",
            }
        )
    return out


def attention_payload(brief: dict, viewer: Viewer, *, this_node: str) -> dict:
    """What a landing or a Needs-you chip renders: the count, the items, and
    where everything else went (reachable, not pushed)."""
    items = attention_items(brief, viewer, this_node=this_node)
    counts = brief.get("counts") or {}
    return {
        "generated_at": brief.get("generated_at"),
        "count": len(items),
        "items": items,
        # Behind a "History" disclosure, never on the landing's face.
        "history": {
            "decided": int(counts.get("cases_decided") or 0),
            "passed": int(counts.get("green") or 0),
            "trust_changes": int(counts.get("deltas") or 0),
            "href": "#/control",
        },
    }


__all__ = [
    "DECIDE_SCOPE",
    "LOCAL_OWNER",
    "OPS_CLAIM_KINDS",
    "OPS_SCOPE",
    "SITE_NODES_ENV",
    "Viewer",
    "attention_items",
    "attention_payload",
    "site_nodes",
    "viewer_from_request",
    "viewer_from_roles",
]
