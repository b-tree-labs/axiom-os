# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A held action carries plain words, and never names a secret parameter (#1876, #1881).

The Control design approved on 2026-11-12 shows a held action as a title, "What will happen" and
"Why it is asking", with a Details disclosure holding the requester, the capability, the harm
class and the parameters. The client may not invent those words, so ``GET
/api/v1/attest/approvals/pending`` sends them:

- LB-HA-A1: ``title``, ``description`` and ``consequence`` as the action stored them when held.
- LB-HA-A2: an action held without them gets them from its capability's declaration (the first
  sentence of its description, the rest of it, and the harm class its risk implies); a capability
  that declares nothing gets nulls, never invented words.
- LB-HA-A3: the dispatch chokepoint stores them on the action when it holds a call.
- SBD-A21 (prd-steer): no held-action view lists a secret parameter by name; one flag,
  ``uses_stored_credential``, says one is used, and appears true only then.
"""

from __future__ import annotations

import json
import logging

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import service  # noqa: E402
from axiom.infra.orchestrator.actions import Action, plain_words  # noqa: E402
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec  # noqa: E402

from .test_x3 import gate, held  # noqa: E402,F401

# ruff: noqa: F811  (pytest fixtures imported from the X3 tests)

SECRET = "s3cret-value"


def _spec(name="reports.archive", risk="destructive", description=None) -> SkillSpec:
    return SkillSpec(
        name=name,
        fn=lambda params, ctx=None: SkillResult(ok=True, value={}),
        description=description
        if description is not None
        else (
            "Archive old reports. Reports older than the cut-off move to cold storage and the "
            "originals are removed."
        ),
        inputs={"older_than": "str", "api_token": "str"},
        side_effects=True,
        risk=risk,
    )


def _pending(client, gate):
    from axiom.extensions.builtins.attest.tests.test_api import OP2, as_

    r = as_(client, OP2).get("/api/v1/attest/approvals/pending")
    assert r.status_code == 200, r.text
    return {a["action_id"]: a for a in r.json()["pending"]}


@pytest.mark.acceptance("LB-HA-A1")
def test_lb_ha_a_held_action_sends_the_words_it_was_held_with(client, gate):
    action = Action(name="reports.archive", params={"older_than": "90d"}, reason="a write",
                    title="Archive last quarter's reports",
                    description="412 reports older than 90 days move to cold storage.",
                    consequence="loss")  # fmt: skip
    gate.submit(action)
    got = _pending(client, gate)[action.action_id]
    assert got["title"] == "Archive last quarter's reports"
    assert got["description"] == "412 reports older than 90 days move to cold storage."
    assert got["consequence"] == "loss"
    assert got["uses_stored_credential"] is False
    # the words survive the durable store
    again = gate.get(action.action_id)
    assert (again.title, again.consequence) == ("Archive last quarter's reports", "loss")


@pytest.mark.acceptance("LB-HA-A2")
def test_lb_ha_words_come_from_the_capability_when_the_action_has_none(client, gate, monkeypatch):
    registry = SkillRegistry()
    registry.register_skill(_spec(), mutating=True)
    registry.register_skill(_spec(name="quiet.thing", risk=None, description=""), mutating=True)
    monkeypatch.setattr(service, "_skill_registry", lambda: registry)
    declared = held(gate, params={"older_than": "90d"}, name="reports.archive")
    bare = held(gate, params={"x": 1}, name="quiet.thing")
    unknown = held(gate, params={"x": 1}, name="not.a.skill")
    got = _pending(client, gate)
    assert got[declared.action_id]["title"] == "Archive old reports"
    assert got[declared.action_id]["description"] == (
        "Reports older than the cut-off move to cold storage and the originals are removed."
    )
    assert got[declared.action_id]["consequence"] == "loss"
    for a in (bare, unknown):
        assert got[a.action_id]["title"] is None
        assert got[a.action_id]["description"] is None
        assert got[a.action_id]["consequence"] is None
    words = plain_words(_spec(risk="reversible-write"), {})
    assert words["consequence"] == "waste"
    assert plain_words(_spec(risk="read"), {})["consequence"] == "noise"


@pytest.mark.acceptance("LB-HA-A3")
def test_lb_ha_the_chokepoint_stores_the_words_when_it_holds_a_call(tmp_path):
    from axiom.infra.orchestrator.approval import ApprovalGate
    from axiom.infra.orchestrator.approval_store import FileActionStore
    from axiom.infra.skill_dispatch import _hold_for_approval

    registry = SkillRegistry()
    registry.register_skill(_spec(), mutating=True)
    ctx = SkillContext(registry=registry, state_dir=tmp_path, logger=logging.getLogger("t"))
    action_id = _hold_for_approval("reports.archive", {"older_than": "90d"}, ctx, reason="r")
    stored = ApprovalGate(FileActionStore(tmp_path / "orchestrator" / "approvals.json")).get(
        action_id
    )
    assert stored.title == "Archive old reports"
    assert stored.consequence == "loss"
    assert stored.description.startswith("Reports older than the cut-off")


@pytest.mark.acceptance("SBD-A21")
def test_sbd_a21_a_secret_parameter_is_never_named_and_one_flag_says_one_is_used(client, gate):
    secret = held(gate, params={"path": "a.txt", "api_token": SECRET,
                                "nested": {"password": SECRET, "keep": 1}})  # fmt: skip
    plain = held(gate, params={"path": "a.txt"})
    got = _pending(client, gate)
    shown = got[secret.action_id]
    assert shown["uses_stored_credential"] is True
    assert got[plain.action_id]["uses_stored_credential"] is False
    wire = json.dumps(shown)
    for word in ("api_token", "password", SECRET, "redacted"):
        assert word not in wire, word
    assert json.loads(shown["summary"]) == {"nested": {"keep": 1}, "path": "a.txt"}
    # the decision draft a person signs shows the same, so it never names it either
    draft_id = service.create_decision_draft(site_id="site-a", action=secret, approving=True,
                                             reason="", origin="human",
                                             for_principal="@op2:site-a")  # fmt: skip
    fields = service.draft(draft_id)["content"]["fields"]
    assert "api_token" not in json.dumps(fields) and "password" not in json.dumps(fields)


@pytest.mark.acceptance("SBD-A21")
def test_sbd_a21_the_pending_skill_says_the_same_on_cli_mcp_and_chat(tmp_path):
    """``approvals.pending`` (CLI, MCP, chat) never names a secret parameter either, and carries
    the same plain words (SBD-A22: one skill on every surface)."""
    from axiom.infra.orchestrator import skills as approval_skills
    from axiom.infra.orchestrator.approval import ApprovalGate
    from axiom.infra.orchestrator.approval_store import FileActionStore
    from axiom.infra.principal import PrincipalContext

    registry = SkillRegistry()
    registry.register_skill(_spec(), mutating=True)
    g = ApprovalGate(FileActionStore(tmp_path / "orchestrator" / "approvals.json"))
    g.submit(Action(name="reports.archive", params={"older_than": "90d", "api_token": SECRET}))
    ctx = SkillContext(registry=registry, state_dir=tmp_path, logger=logging.getLogger("t"),
                       principal=PrincipalContext(handle="@op2:site-a"))  # fmt: skip
    got = approval_skills.pending({}, ctx).value["pending"][0]
    assert got["params"] == {"older_than": "90d"}
    assert got["uses_stored_credential"] is True
    assert got["title"] == "Archive old reports" and got["consequence"] == "loss"
    assert SECRET not in json.dumps(got) and "api_token" not in json.dumps(got)
