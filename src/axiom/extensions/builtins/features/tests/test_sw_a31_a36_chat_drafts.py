# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The chat side of dual drive (SW-A31 to SW-A36, screens b-dual-*).

A site admin asks chat for a cohort in their own words. The answer carries one draft card: the
rule, the live count and two offered actions, "Open in builder" and "Apply", as ``ui_command``
frames on the existing dual-drive path (``view_drive`` -> ``SseRenderProvider``). Chat never
applies: it has no tool that writes a saved cohort or rollout, and only the person's press on
Apply does. An amendment to a draft already open carries an ``auto`` receipt with the body before,
so the page can undo it. An apply of a draft chat last changed is audited ``via: chat``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.features.tests.conftest import web

BODY = {"name": "Weekday pilots", "description": "",
        "rules": {"all": [{"attr": "site_weekday", "op": "in", "value": ["mon", "tue"]},
                          {"attr": "locale", "op": "eq", "value": "en-US"}]},
        "principals": ["dee"], "accounts": []}


@pytest.fixture
def chat(people, monkeypatch):
    """The chat surface as the in-app chat runs it: ``@ana:acct``'s turn, the flags skills, the
    SSE frames the page reads."""
    from axiom.extensions.builtins.chat import view_drive
    from axiom.extensions.builtins.chat.providers.sse_render import SseRenderProvider
    from axiom.extensions.builtins.features import attributes
    from axiom.extensions.builtins.features.skills import bind
    from axiom.infra.skills import SkillRegistry

    monkeypatch.setenv("AXIOM_SITE_TZ", "America/Chicago")
    attributes.set_site_settings({})
    reg = SkillRegistry()
    bind(reg)
    scope = SimpleNamespace(session=SimpleNamespace(principal_id="@ana:acct"), view_context=None, ui_commands=[])
    token = view_drive.ACTIVE_SCOPE.set(scope)
    frames: list[dict] = []
    render = SseRenderProvider(frames.append)
    yield {**people, "registry": reg, "scope": scope, "frames": frames, "render": render}
    view_drive.ACTIVE_SCOPE.reset(token)


def _tool(chat, capability, params):
    """One chat tool call, as the turn loop makes it, and its frames."""
    from axiom.extensions.builtins.chat.tools import _execute_skill_tool
    from axiom.infra.capability_projection import capability_to_surface_name

    name = capability_to_surface_name(capability)
    result = _execute_skill_tool(name, params, registry=chat["registry"])
    chat["render"].render_tool_result(name, result, 0.1)
    return result


def _sent(chat):
    return [f["ui_command"] for f in chat["frames"] if "ui_command" in f]


@pytest.mark.acceptance("SW-A31")
def test_sw_a31_a_draft_from_chat_carries_one_card_with_open_and_apply(chat):
    """SW-A31: a draft chat writes is the person's own (one draft, the builder's), and the answer
    offers "Open in builder" and "Apply"; nothing is applied."""
    out = _tool(chat, "flags.cohort_draft_create", {"key": "pilots", "body": BODY})
    assert out["ok"], out
    draft = out["value"]["draft"]
    assert draft["updated_by"] == "@ana:acct" and draft["updated_via"] == "chat"
    assert _sent(chat) == [
        {"type": "flag_draft.open", "label": "Open in builder", "kind": "cohort", "key": "pilots", "revision": 1},
        {"type": "flag_draft.apply", "label": "Apply", "kind": "cohort", "key": "pilots", "revision": 1},
    ]
    assert chat["scope"].ui_commands == _sent(chat)
    assert chat["targeting"].cohort("pilots") is None


@pytest.mark.acceptance("SW-A31")
def test_sw_a31_the_builder_reads_the_draft_chat_wrote(chat, monkeypatch):
    """SW-A31: one object: the builder's GET returns the draft chat saved, revision and all."""
    _tool(chat, "flags.cohort_draft_create", {"key": "pilots", "body": BODY})
    r = web(monkeypatch, sub="ana", roles=("site-admin",), site="acct").get("/api/v1/flags/drafts/cohorts/pilots")
    assert r.status_code == 200 and r.json()["draft"]["updated_via"] == "chat"


@pytest.mark.acceptance("SW-A31")
def test_sw_a31_a_rollout_draft_gets_the_same_pair(chat):
    """SW-A31: the same two offers for a rollout draft, kind rollout."""
    body = {"rules": [{"value": "on", "percentage": 10}], "bucket_by": None, "environment": "development"}
    _tool(chat, "flags.rollout_draft_create", {"key": "beta_editor", "body": body})
    assert [(c["type"], c["kind"], c["key"]) for c in _sent(chat)] == [
        ("flag_draft.open", "rollout", "beta_editor"), ("flag_draft.apply", "rollout", "beta_editor")]


@pytest.mark.acceptance("SW-A34")
def test_sw_a34_an_amendment_carries_an_undoable_receipt(chat):
    """SW-A34: chat amending a draft also sends ``flag_draft.amended`` (auto) with one line of what
    changed and the body before, which the page PUTs back on Undo."""
    _tool(chat, "flags.cohort_draft_create", {"key": "pilots", "body": BODY})
    chat["frames"].clear()
    amended = {**BODY, "rules": {"all": [{"attr": "locale", "op": "eq", "value": "en-US"}]}}
    out = _tool(chat, "flags.cohort_draft_update", {"key": "pilots", "body": amended, "revision": 1})
    assert out["ok"], out
    sent = _sent(chat)
    receipt = next(c for c in sent if c["type"] == "flag_draft.amended")
    assert receipt["auto"] is True and receipt["kind"] == "cohort" and receipt["key"] == "pilots"
    assert receipt["revision"] == 2 and receipt["summary"] == "Removed the site weekday condition"
    assert receipt["before"]["rules"] == BODY["rules"]
    assert [c["type"] for c in sent if c["type"] != "flag_draft.amended"] == ["flag_draft.open", "flag_draft.apply"]


@pytest.mark.acceptance("SW-A31", "SW-A34")
def test_sw_a31_a34_no_card_off_chat(chat, monkeypatch):
    """The card is chat's: the builder, MCP and the command line get no ui_commands."""
    from axiom.extensions.builtins.features.tests.conftest import run

    out = run("flags.cohort_draft_create", {"key": "pilots", "body": BODY}, chat["state"], actor="@ana:acct",
              surface="mcp")
    assert out.ok and "ui_commands" not in out.value
    r = web(monkeypatch, sub="ana", roles=("site-admin",), site="acct").put(
        "/api/v1/flags/drafts/cohorts/other", json={"body": BODY})
    assert r.status_code == 200 and "ui_commands" not in r.json()["draft"]


@pytest.mark.acceptance("SW-A33")
def test_sw_a33_chat_cannot_change_a_saved_cohort_only_draft_one(chat):
    """SW-A33: no chat tool writes a saved cohort, rollout, stage plan or import; asked to change a
    saved cohort, chat drafts it, starting from what is saved."""
    from axiom.extensions.builtins.chat.skill_tools import skills_to_tool_definitions

    offered = {t.name for t in skills_to_tool_definitions(chat["registry"], namespace="flags")}
    for writer in ("flags__cohort_set", "flags__cohort_delete", "flags__cohort_import", "flags__rollout_set",
                   "flags__stage_plan_set", "flags__approve"):
        assert writer not in offered, writer
    for draft in ("flags__cohort_draft_create", "flags__cohort_draft_update", "flags__rollout_draft_create",
                  "flags__values", "flags__cohort_preview"):
        assert draft in offered, draft
    chat["targeting"].put_cohort("pilots", name="Pilots", principals=["dee", "eve"], by="@t")
    out = _tool(chat, "flags.cohort_draft_create", {"key": "pilots"})
    assert out["ok"], out
    assert out["value"]["draft"]["body"]["principals"] == ["dee", "eve"]
    assert out["value"]["draft"]["body"]["name"] == "Pilots"
    assert chat["targeting"].cohort("pilots")["principals"] == ["dee", "eve"]


@pytest.mark.acceptance("SW-A35")
def test_sw_a35_words_it_cannot_map_become_one_question_with_listed_choices(chat):
    """SW-A35: the tools tell the model to ask one plain question whose choices come from
    flags.values, as an offer sentence then a short markdown list, and never to guess."""
    from axiom.extensions.builtins.chat.skill_tools import skills_to_tool_definitions

    tools = {t.name: t for t in skills_to_tool_definitions(chat["registry"], namespace="flags")}
    for name in ("flags__values", "flags__cohort_draft_create", "flags__cohort_draft_update"):
        text = tools[name].description.lower()
        assert "one plain question" in text and "markdown list" in text and "never guess" in text, name
    out = _tool(chat, "flags.values", {"attribute": "site_weekday"})
    assert [v["value"] for v in out["value"]["values"]][:2] == ["mon", "tue"]


@pytest.mark.acceptance("SW-A35")
def test_sw_a35_a_stub_model_asks_instead_of_guessing(chat, tmp_path, monkeypatch):
    """SW-A35: driving the turn with a stub model, the flags tools reach it with the guidance, and a
    question answer goes to the page as words, with no draft written."""
    from axiom.extensions.builtins.chat import tools as chat_tools
    from axiom.extensions.builtins.chat.headless import HeadlessChat
    from axiom.extensions.builtins.chat.providers.sse_render import SseRenderProvider
    from axiom.infra.bus import EventBus
    from axiom.infra.gateway import CompletionResponse, Gateway

    monkeypatch.setattr(chat_tools, "_skill_namespaces", lambda: ["flags"])
    monkeypatch.setattr(chat_tools, "_skill_registry", lambda: chat["registry"])
    gw = MagicMock(spec=Gateway)
    gw.available = True
    gw.active_provider = MagicMock()
    gw.active_provider.name, gw.active_provider.model = "test", "test-model"
    question = ("Which region do you mean? I can offer these:\n- US-TX\n- US-OK")
    gw.complete_with_tools.return_value = CompletionResponse(text=question, provider="test", success=True)
    frames: list[dict] = []
    session = HeadlessChat(turn_deadline=30.0, gateway=gw, bus=EventBus(log_path=tmp_path / "e.jsonl"))
    session.agent.set_render_provider(SseRenderProvider(frames.append))
    scope = session.new_scope()
    text = session.turn("make a cohort of people up north", scope=scope, stream=False)
    offered = {t["function"]["name"]: t["function"].get("description", "")
               for call in gw.complete_with_tools.call_args_list for t in call.kwargs.get("tools") or []}
    assert "flags__values" in offered and "one plain question" in offered["flags__values"].lower()
    assert "- US-TX" in text and not [f for f in frames if "ui_command" in f]
    assert chat["targeting"].cohort("pilots") is None


@pytest.mark.acceptance("SW-A32")
def test_sw_a32_the_open_builder_reaches_chat_as_view_context(chat):
    """SW-A32: the builder describes its draft in view_context; chat keeps it bounded and reads it
    in the prompt, so "What did I change?" is answered from the view."""
    from axiom.extensions.builtins.chat import view_drive as vd

    raw = {"schema": vd.SCHEMA, "route": "#/settings/flags/cohorts/pilots", "screen": "cohort-builder",
           "actions": [], "summary": "Editing cohort pilots.",
           "draft": {"kind": "cohort", "key": "pilots", "revision": 3, "body": BODY,
                     "saved": {**BODY, "principals": []},
                     "changes": ["Added dee", "Added the site weekday condition"]}}
    view = vd.parse_view_context(raw)
    assert view["draft"]["key"] == "pilots" and view["draft"]["revision"] == 3
    assert view["draft"]["changes"] == ["Added dee", "Added the site weekday condition"]
    prompt = vd.render_prompt(view)
    assert "draft cohort pilots" in prompt.lower() and "Added the site weekday condition" in prompt
    assert "site_weekday" in prompt  # the rule itself
    huge = vd.parse_view_context({**raw, "draft": {**raw["draft"], "body": {"principals": ["p"] * 50000}}})
    assert huge.get("truncated") is True


@pytest.mark.acceptance("SW-A36")
def test_sw_a36_an_apply_of_a_draft_chat_last_changed_is_audited_by_chat(chat, monkeypatch):
    """SW-A36: the person's Apply of a draft chat last changed is audited ``via: chat``; an apply of
    a draft the builder last changed, or of no draft, is ``via: web``."""
    from axiom.infra import audit_trail

    _tool(chat, "flags.cohort_draft_create", {"key": "pilots", "body": BODY})
    client = web(monkeypatch, sub="ana", roles=("site-admin",), site="acct")
    preview = client.post("/api/v1/flags/cohorts/pilots/preview", json=BODY).json()
    r = client.post("/api/v1/flags/cohorts/pilots/apply",
                    json={**BODY, "plan_hash": preview["plan_hash"], "draft_revision": 1})
    assert r.status_code == 200, r.text
    other = {**BODY, "principals": ["eve"]}
    client.put("/api/v1/flags/drafts/cohorts/team", json={"body": other})
    preview = client.post("/api/v1/flags/cohorts/team/preview", json=other).json()
    assert client.post("/api/v1/flags/cohorts/team/apply",
                       json={**other, "plan_hash": preview["plan_hash"], "draft_revision": 1}).status_code == 200
    records = audit_trail.read_chain(audit_trail.audit_path(chat["state"]))
    via = {r["change"]["subject"]: r["change"]["via"] for r in records
           if r.get("change") and r["change"]["kind"] == "cohort"}
    assert via == {"pilots": "chat", "team": "web"}
