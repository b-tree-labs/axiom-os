# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Renewing another harness's session is an IMPORT: a new native session with provenance; the source is untouched;
the same source again updates the same session; and unclassified imports are held back from cloud providers."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat.commands_ext import renew, renew_sources
from axiom.infra.orchestrator.session import Session

from ._renew_harness_support import CODEX_ID, claude_session, codex_session, fingerprint
from ._renew_support import ME, Agent, FakeGateway, make_session, new_store


@pytest.fixture
def rig(tmp_path, _no_foreign_harness_state, monkeypatch):
    monkeypatch.setattr("axiom.infra.orchestrator.session._default_principal", lambda: ME)
    monkeypatch.setattr("axiom.infra.orchestrator.session.may_adopt", lambda p: False)
    home = _no_foreign_harness_state
    codex_session(home)
    claude_session(home)
    store = new_store(tmp_path)
    return store, Agent(Session(principal_id=ME)), home


def _natives(store):
    return [s for s in store.list_sessions() if "renewed_from" in (store.peek(s) or {}).get("context", {})]


def test_renewing_a_foreign_session_creates_a_new_native_one_with_converted_messages(rig):
    store, agent, _ = rig
    out = renew.renew(agent, store, "flux")
    session = agent.session
    assert session.session_id in _natives(store)
    assert [m.role for m in session.messages] == ["user", "assistant", "user", "assistant"]
    assert session.messages[0].content == "Check the flux log"
    assert "[tool call: shell(command=cat flux.log)]" in session.messages[1].content  # kept as text, not dropped
    assert session.principal_id == ME
    assert "renewed Check the flux log from codex (4 messages, imported, source unchanged)" in out
    assert "tool calls kept as text summaries (1 calls, 1 results" in out  # says what did not map
    assert "/where" in out and "/context" in out                           # the next steps


def test_the_provenance_marks_it_as_imported_so_it_is_never_taken_for_native(rig):
    store, agent, home = rig
    renew.renew(agent, store, "flux")
    prov = agent.session.context["renewed_from"]
    assert prov["harness"] == "codex" and prov["source_session_id"] == CODEX_ID
    assert prov["message_count"] == 4
    assert len(prov["source_path_hash"]) == 16 and str(home) not in prov["source_path_hash"]
    assert prov["imported_at"].startswith("20")
    persisted = store.peek(agent.session.session_id)["context"]["renewed_from"]
    assert persisted["source_session_id"] == CODEX_ID
    # and the list says so: native copy is labelled, the foreign row says it was imported
    rows = renew.aggregate_rows(store, Session(principal_id=ME))
    copy = next(r for r in rows if r["id"] == agent.session.session_id)
    original = next(r for r in rows if r["source"] == "codex")
    assert copy["renewed_from"] == "codex" and original["imported_as"] == copy["id"]
    assert "(renewed from codex)" in "\n".join(renew.render_rows([copy]))
    assert "(imported)" in "\n".join(renew.render_rows([original]))


def test_the_source_files_are_byte_and_mtime_identical_after_a_renewal(rig):
    store, agent, home = rig
    before = fingerprint(home)
    renew.renew(agent, store, "flux")
    renew.renew(agent, store, "calibration")
    renew.renew(agent, store, "flux")
    assert fingerprint(home) == before


def test_renewing_the_same_source_again_updates_the_same_session_and_does_not_duplicate(rig):
    store, agent, _ = rig
    renew.renew(agent, store, "flux")
    first_id = agent.session.session_id
    other = Agent(Session(principal_id=ME))
    out = renew.renew(other, store, f"codex:{CODEX_ID}")
    assert other.session.session_id == first_id
    assert len(_natives(store)) == 1
    assert "updated the earlier import" in out


def test_a_refresh_picks_up_new_source_turns_and_keeps_what_was_said_after_the_import(rig):
    store, agent, home = rig
    renew.renew(agent, store, "flux")
    agent.session.add_message("user", "my own follow-up")
    agent.session.add_message("assistant", "my own answer")
    store.save(agent.session)
    # the source grows by one exchange
    path = next(home.rglob(f"rollout-*{CODEX_ID}.jsonl"))
    import json

    extra = [
        {"timestamp": "2026-10-02T09:05:00.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "One more"}]}},
        {"timestamp": "2026-10-02T09:05:01.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Added"}]}},
    ]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in extra))
    out = renew.renew(agent, store, f"codex:{CODEX_ID}")
    texts = [m.content for m in agent.session.messages]
    assert texts[4:6] == ["One more", "Added"]
    assert texts[6:] == ["my own follow-up", "my own answer"]
    assert agent.session.context["renewed_from"]["message_count"] == 6
    assert len(_natives(store)) == 1
    assert "2 messages said since then were kept" in out


def test_a_session_that_is_gone_from_the_source_is_reported_not_invented(rig):
    store, agent, _ = rig
    out = renew.resume_session(store, agent, "codex:019e0000-dead-beef-0000-000000000000")
    assert "not found" in out and _natives(store) == []


# ---- the tier hold ------------------------------------------------------------------------------


def _providers(tmp_path):
    import os

    from axiom.llm.gateway import LLMProvider

    os.environ["RENEW_TEST_CLOUD_KEY"] = "k"
    return [
        LLMProvider(name="home-llm", endpoint="http://127.0.0.1:11434/v1", model="m"),
        LLMProvider(name="cloud-llm", endpoint="https://api.example-llm.com/v1", model="m",
                    api_key_env="RENEW_TEST_CLOUD_KEY"),
    ]


def test_an_import_is_unconfirmed_and_a_cloud_provider_may_not_receive_it_until_confirmed(rig, tmp_path):
    store, agent, _ = rig
    out = renew.renew(agent, store, "flux")
    assert "no classification" in out and "/renew confirm-tier" in out
    assert agent.session.context["renewed_from"]["tier_confirmed"] is False
    refusal = renew_sources.send_refusal(agent.session, FakeGateway(*_providers(tmp_path)))
    assert "Not sent" in refusal and "cloud-llm" in refusal and "/renew confirm-tier" in refusal
    assert "home-llm" not in refusal.split("(")[1]
    confirmed = renew.renew(agent, store, "confirm-tier")
    assert "Confirmed" in confirmed
    assert store.peek(agent.session.session_id)["context"]["renewed_from"]["tier_confirmed"] is True
    assert renew_sources.send_refusal(agent.session, FakeGateway(*_providers(tmp_path))) == ""


def test_with_only_local_providers_an_unconfirmed_import_is_not_blocked(rig, tmp_path):
    store, agent, _ = rig
    renew.renew(agent, store, "flux")
    local_only = FakeGateway(_providers(tmp_path)[0])
    assert renew_sources.send_refusal(agent.session, local_only) == ""


def test_the_hold_covers_the_fallback_chain_not_just_the_first_choice(rig, tmp_path):
    store, agent, _ = rig
    renew.renew(agent, store, "flux")
    first_is_local = FakeGateway(*_providers(tmp_path))  # local first, cloud behind it
    assert first_is_local.active_provider.name == "home-llm"
    assert renew_sources.send_refusal(agent.session, first_is_local) != ""


def test_a_native_session_is_never_held(rig, tmp_path):
    store, agent, _ = rig
    native = make_session(store, "nat000000001", "native")
    assert renew_sources.send_refusal(native, FakeGateway(*_providers(tmp_path))) == ""


def test_changed_source_content_needs_the_confirmation_again(rig):
    store, agent, home = rig
    renew.renew(agent, store, "flux")
    renew.renew(agent, store, "confirm-tier")
    path = next(home.rglob(f"rollout-*{CODEX_ID}.jsonl"))
    import json

    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"timestamp": "2026-10-02T09:09:00Z", "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "new material"}]}}) + "\n")
    renew.renew(agent, store, f"codex:{CODEX_ID}")
    assert agent.session.context["renewed_from"]["tier_confirmed"] is False


def test_confirming_outside_an_import_says_there_is_nothing_to_confirm(rig):
    store, agent, _ = rig
    assert "Nothing to confirm" in renew.renew(agent, store, "confirm-tier")


def test_the_agent_refuses_a_turn_for_an_unconfirmed_import_before_any_model_call(rig, tmp_path):
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.infra.bus import EventBus
    from axiom.llm.gateway import Gateway

    store, probe, _ = rig
    renew.renew(probe, store, "flux")
    gateway = Gateway(config_dir=tmp_path / "gw")
    gateway.providers = _providers(tmp_path)
    agent = ChatAgent(gateway=gateway, bus=EventBus(log_path=tmp_path / "e.jsonl"), session=probe.session)
    before = len(probe.session.messages)
    reply = agent.turn("what did we conclude?", stream=False)
    assert "Not sent" in reply and "/renew confirm-tier" in reply
    assert len(probe.session.messages) == before          # nothing was added, nothing was sent
    gateway.providers = [_providers(tmp_path)[0]]
    assert renew_sources.send_refusal(agent.session, gateway) == ""
