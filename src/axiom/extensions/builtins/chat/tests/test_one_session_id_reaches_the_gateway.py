# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One conversation id reaches the gateway (#2226, phase 0 of #2214).

The conversation id is the chat ``Session.session_id``. A chat turn binds it to
the trace context for the length of the turn, on every surface, and unbinds it
afterwards, so the gateway's ``current_session()`` names the conversation that
asked and never the one before it on the same worker.

One test per acceptance row: SID-A1, SID-A2, SID-A3 and SID-A5 here; SID-A4
(the bridge) is in ``tests/test_bridge_session_id.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.extensions.builtins.chat.headless import HeadlessChat
from axiom.infra import trace
from axiom.infra.bus import EventBus
from axiom.infra.gateway import CompletionResponse, Gateway, StreamChunk
from axiom.infra.orchestrator.session import Session


class _Recorder:
    """A gateway double that writes down the session id each call ran under."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.on_call = None  # optional hook run inside each gateway call
        gw = MagicMock(spec=Gateway)
        gw.available = True
        gw.active_provider = MagicMock()
        gw.active_provider.name = "test"
        gw.active_provider.model = "test-model"
        gw.complete_with_tools.side_effect = self._complete
        gw.stream_with_tools.side_effect = self._stream
        self.gateway = gw

    def _note(self) -> None:
        self.seen.append(trace.current_session())
        if self.on_call is not None:
            self.on_call()

    def _complete(self, *_a, **_kw) -> CompletionResponse:
        self._note()
        return CompletionResponse(text="ok", provider="test", success=True)

    def _stream(self, *_a, **_kw) -> Iterator[StreamChunk]:
        # A generator body runs where it is iterated, which is the point: the
        # terminal iterates the provider stream on a worker thread.
        def gen() -> Iterator[StreamChunk]:
            self._note()
            yield StreamChunk(type="text", text="ok")
            yield StreamChunk(type="done")

        return gen()


@pytest.fixture
def rec() -> _Recorder:
    return _Recorder()


def _terminal_agent(rec: _Recorder, tmp_path: Path, session: Session) -> ChatAgent:
    agent = ChatAgent(gateway=rec.gateway, bus=EventBus(log_path=tmp_path / "events.jsonl"),
                      session=session)
    agent.set_renderer(lambda chunks: "".join(c.text or "" for c in chunks))
    return agent


# ---------------------------------------------------------------------------
# SID-A1: same session, same id; different sessions, different ids; per surface
# ---------------------------------------------------------------------------


def test_sid_a1_terminal_two_turns_one_id_two_sessions_two_ids(rec, tmp_path):
    """The terminal streams, so the provider is iterated on a worker thread."""
    one, two = Session(), Session()
    a = _terminal_agent(rec, tmp_path, one)
    a.turn("first", stream=True)
    a.turn("second", stream=True)
    b = _terminal_agent(rec, tmp_path, two)
    b.turn("first", stream=True)

    assert rec.seen == [one.session_id, one.session_id, two.session_id]
    assert one.session_id != two.session_id


def test_sid_a1_web_two_turns_one_id_two_sessions_two_ids(rec, tmp_path):
    """The web surface runs a headless turn per request on a scope it builds."""
    headless = HeadlessChat(turn_deadline=30.0, gateway=rec.gateway,
                            bus=EventBus(log_path=tmp_path / "events.jsonl"))
    one, two = Session(), Session()
    headless.turn("first", stream=False, scope=headless.new_scope(session=one))
    headless.turn("second", stream=False, scope=headless.new_scope(session=one))
    headless.turn("first", stream=False, scope=headless.new_scope(session=two))

    assert rec.seen == [one.session_id, one.session_id, two.session_id]
    assert one.session_id != two.session_id


def test_sid_a1_http_chat_two_turns_one_id_two_callers_two_ids(rec, monkeypatch):
    """The HTTP chat server keeps one agent, so one Session, per caller."""
    from axiom.extensions.builtins.http import chat_server
    from axiom.infra import gateway as gateway_mod

    monkeypatch.setattr(gateway_mod, "Gateway", lambda *a, **k: rec.gateway)
    monkeypatch.setattr(chat_server, "_agents", type(chat_server._agents)())

    alice = chat_server._get_agent({"eid": "alice"})
    alice.turn("first", stream=False)
    chat_server._get_agent({"eid": "alice"}).turn("second", stream=False)
    bob = chat_server._get_agent({"eid": "bob"})
    bob.turn("first", stream=False)

    assert rec.seen == [alice.session.session_id, alice.session.session_id,
                        bob.session.session_id]
    assert alice.session.session_id != bob.session.session_id


# ---------------------------------------------------------------------------
# SID-A2: the id does not outlive its turn
# ---------------------------------------------------------------------------


def test_sid_a2_the_next_request_on_the_worker_does_not_see_the_last_session(rec, tmp_path):
    assert trace.current_session() == "anonymous"
    a = _terminal_agent(rec, tmp_path, Session())
    a.turn("first", stream=False)
    assert trace.current_session() == "anonymous"

    # A turn that fails is unbound too.
    rec.gateway.complete_with_tools.side_effect = RuntimeError("provider down")
    try:
        a.turn("second", stream=False)
    except RuntimeError:
        pass
    assert trace.current_session() == "anonymous"


# ---------------------------------------------------------------------------
# SID-A3: the gateway routing row and the agent routing audit row carry it
# ---------------------------------------------------------------------------


def test_sid_a3_both_routing_rows_carry_the_turns_session_id(rec, tmp_path, monkeypatch):
    from axiom.infra import audit_log, routing_audit

    gateway_rows: list[str] = []
    agent_rows: list[str] = []

    class _Audit:
        def write_routing(self, *, session_id: str, **_fields) -> None:
            gateway_rows.append(session_id)

    monkeypatch.setattr(audit_log.AuditLog, "get", classmethod(lambda cls: _Audit()))
    monkeypatch.setattr(
        routing_audit, "log_routing_decision",
        lambda *, session_id="", **_kw: agent_rows.append(session_id) or True,
    )
    # The real gateway method that writes the routing row, run inside the call.
    rec.on_call = lambda: Gateway._write_routing_audit(MagicMock(), tier_requested="any")

    session = Session()
    _terminal_agent(rec, tmp_path, session).turn("hello", stream=True)

    assert gateway_rows == [session.session_id]
    assert agent_rows == [session.session_id]
    assert session.session_id not in ("", "anonymous")


# ---------------------------------------------------------------------------
# SID-A5: the memory session is unchanged; a fragment records the conversation
# ---------------------------------------------------------------------------


def _composition(base: Path):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    base.mkdir(parents=True, exist_ok=True)
    kp = generate_keypair()
    return CompositionService(
        artifact_registry=ArtifactRegistry(backend=SQLiteBackend(base / "artifacts.db")),
        audit_log=AuditLog(base / "audit.jsonl", signing_keypair=kp),
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


def test_sid_a5_memory_session_unchanged_and_fragment_records_the_conversation(
    rec, tmp_path, monkeypatch,
):
    from axiom.memory import session as memory_session

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXI_DISABLE_SESSION", "0")
    monkeypatch.delenv("AXI_SESSION_ID", raising=False)
    memory_session.reset_for_tests()
    try:
        memory_before = memory_session.current_session_id()
        comp = _composition(tmp_path / "mem")
        written = []
        rec.on_call = lambda: written.append(comp.write(
            content={"event_time": "2026-11-21T10:00:00+00:00", "text": "noted"},
            cognitive_type="episodic",
            principal_id="@alice:home",
            agents={"axi-chat"},
            resources=set(),
            accountable_human_id="@alice:home",
        ))

        chat = Session()
        _terminal_agent(rec, tmp_path, chat).turn("remember this", stream=False)

        assert memory_session.current_session_id() == memory_before
        assert memory_before.startswith("session://")
        (frag,) = written
        assert frag.provenance.session_id == memory_before
        assert frag.provenance.conversation_id == chat.session_id
        # It is stored, not only held in memory: read it back.
        (stored,) = comp.read([frag.id], user="@alice:home", agent="axi-chat")
        assert stored.provenance.conversation_id == chat.session_id
    finally:
        memory_session.reset_for_tests()


def test_sid_a5_a_fragment_written_outside_a_turn_records_no_conversation(tmp_path):
    comp = _composition(tmp_path / "mem")
    frag = comp.write(
        content={"event_time": "2026-11-21T10:00:00+00:00"},
        cognitive_type="episodic",
        principal_id="@alice:home",
        agents=set(),
        resources=set(),
        accountable_human_id="@alice:home",
    )
    assert frag.provenance.conversation_id == ""
    # And a fragment without one keeps its old encoding (signatures over it hold).
    assert "conversation_id" not in frag.to_dict()["provenance"]
