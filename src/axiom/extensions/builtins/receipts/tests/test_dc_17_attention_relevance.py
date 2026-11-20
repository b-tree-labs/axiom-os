# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""DC-17: a surface pushes an item only if it is high-value for the viewer's
role(s) or carries an action this viewer can take now, and the action works.
Prominence is personal; reachability is uniform.

Row ids (docs/specs/spec-webapp-buildout-mvp.md):

- MVP-21-A9  no item without a role relevance or a permitted, working action
- MVP-21-A10 node health is ONE item per node, for the roles that run nodes
- MVP-21-A11 no other node's telemetry on a site's landing
- MVP-21-A12 nothing is removed: /today still carries every case
- MVP-02-A9  no meta-status rows or trust changes on the landing payload
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.receipts.relevance import (
    LOCAL_OWNER,
    SITE_NODES_ENV,
    Viewer,
    attention_items,
    attention_payload,
    viewer_from_roles,
)

THIS = "node-a"


def _case(node, kinds, *, status="stale", can_run=True, fix=True):
    return {
        "case_id": f"c-{node}",
        "entity_kind": "node",
        "entity_id": node,
        "title": f"{node} stopped reporting",
        "severity": status,
        "items": [{"claim_kind": k, "status": status} for k in kinds],
        "handling": {
            "can_run": can_run,
            "fix_summary": "Make this node report now.",
            "runs": "fleet.report",
            "because": "",
        },
        "options": ([{"choice": "fix", "label": "Fix", "runs": "fleet.report"}] if fix else [])
        + [{"choice": "hold", "label": "Hold"}],
    }


BRIEF = {
    "generated_at": "2026-10-31T12:00:00+00:00",
    "cases": [
        _case(THIS, ["heartbeat", "backup", "service_health"]),
        _case("dev-laptop", ["heartbeat"]),
    ],
    "trust_deltas": [
        {"entity_id": THIS, "claim_kind": "heartbeat", "from": "green", "to": "stale"}
    ],
    "not_yet_wired": ["agent sessions"],
    "quiet_line": "2 cases already decided, and 1 check passed quietly.",
    "counts": {"cases_decided": 2, "green": 1, "deltas": 1},
}

PERSONAS = {
    "student": ("reader",),
    "researcher": ("contributor",),
    "site-admin": ("site-admin",),
    "admin": ("admin",),
    "owner": ("owner",),
}


def _viewer(persona):
    return viewer_from_roles(PERSONAS[persona])


@pytest.mark.parametrize("persona", ["student", "researcher"])
def test_mvp_21_a9_a_reader_is_pushed_no_node_health(persona):
    assert attention_items(BRIEF, _viewer(persona), this_node=THIS) == []


def test_mvp_21_a9_an_unresolved_viewer_is_pushed_nothing():
    assert attention_items(BRIEF, Viewer(), this_node=THIS) == []


@pytest.mark.parametrize("persona", ["site-admin", "owner"])
def test_mvp_21_a10_node_health_is_one_item_with_a_working_action(persona):
    [item] = attention_items(BRIEF, _viewer(persona), this_node=THIS)
    assert item["title"] == "node-a health: 3 stale reports"
    assert item["kind"] == "node_health"
    assert item["action"] == {
        "label": "Make this node report now.",
        "choice": "fix",
        "case_id": "c-node-a",
        "runs": "fleet.report",
    }
    assert item["href"] == "#/control?case=c-node-a"  # the whole case, one click


def test_mvp_21_a10_an_admin_sees_it_but_gets_no_button_the_gate_would_refuse():
    # admin governs and reads nothing: receipts:invoke is not theirs, so the
    # decision POST would be refused. Role-relevant, so shown, with a link.
    [item] = attention_items(BRIEF, _viewer("admin"), this_node=THIS)
    assert item["action"] is None
    assert item["href"]


def test_mvp_21_a10_a_fix_that_would_not_run_is_no_action():
    brief = {**BRIEF, "cases": [_case(THIS, ["heartbeat"], can_run=False)]}
    [item] = attention_items(brief, _viewer("site-admin"), this_node=THIS)
    assert item["action"] is None


def test_mvp_21_a11_no_other_nodes_telemetry_on_the_landing():
    items = attention_items(BRIEF, LOCAL_OWNER, this_node=THIS)
    assert [i["id"] for i in items] == ["c-node-a"]


def test_mvp_21_a11_a_declared_site_node_is_ours(monkeypatch):
    monkeypatch.setenv(SITE_NODES_ENV, "dev-laptop")
    items = attention_items(BRIEF, LOCAL_OWNER, this_node=THIS)
    assert {i["id"] for i in items} == {"c-node-a", "c-dev-laptop"}


def test_mvp_02_a9_the_landing_payload_carries_no_meta_status_or_trust_rows():
    p = attention_payload(BRIEF, _viewer("site-admin"), this_node=THIS)
    assert set(p) == {"generated_at", "count", "items", "history"}
    assert p["count"] == len(p["items"]) == 1
    # Relocated, not deleted: counted behind a History disclosure that opens
    # the full brief.
    assert p["history"] == {"decided": 2, "passed": 1, "trust_changes": 1, "href": "#/control"}
    flat = repr(p)
    assert "not yet watched" not in flat and "agent sessions" not in flat


def test_mvp_21_a12_the_gate_never_mutates_the_brief():
    before = repr(BRIEF)
    attention_payload(BRIEF, _viewer("student"), this_node=THIS)
    assert repr(BRIEF) == before
