# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The client-sink EC gate must not lie about a mutation, and a withhold must be auditable.

These are the three defects reproduced live on 2026-10-06 against a memory
append through the composed MCP surface, with the gate returning
``{"refused": true}`` while the fragment was in fact committed to the ledger.

1. **The sink gate classifies the OUTBOUND result, not the client's own
   echoed-in arguments.** The gate exists to stop export-controlled content
   flowing OUT to a non-EC-capable client. A tool's arguments came IN from that
   client; echoing them back, or acking them, exfiltrates nothing. Folding the
   arguments into the sink classification is what refused a benign billing-note
   append whose own result was a bare ``{fragment_id, ...}`` ack.

2. **A mutation that COMMITTED is never reported ``refused``.** The gate runs
   the tool locally (in-enclave, safe) BEFORE it classifies — so by the time a
   withhold is decided, a write has already happened. Withholding the result
   bytes from a cloud client is correct; telling the client the operation was
   refused is a lie that invites a duplicate retry. For a mutation the envelope
   says ``committed``, with the result withheld.

3. **Every withhold is auditable by the id it returns.** The gate hands the
   client a ``routing_event_id``; that id must resolve. Before this, the
   enforced-withhold branch logged nothing at all, the routing-audit log did
   not even store the id, and ``audit explain`` read a different store — so the
   id the client was given resolved nowhere.

Since ADR-175 the gate withholds on a stored non-public label only and no longer
classifies result text, so each withhold below is triggered by a label. The
three invariants are unchanged.
"""

from __future__ import annotations

import asyncio

from axiom.extensions.builtins.mcp import routing as R
from axiom.llm.router import RoutingDecision, RoutingTier


class _FlagRouter:
    """Classifies EC iff a sentinel appears in the text; otherwise public.

    ``classifier`` is the attributed source — ``keyword`` is definitive (a
    control-list match), ``ollama`` is the advisory small model.
    """

    def __init__(self, sentinel: str = "ECSENTINEL", classifier: str = "keyword"):
        self.sentinel = sentinel
        self.classifier = classifier

    def classify(self, text: str, sensitivity: str = "strict") -> RoutingDecision:
        if self.sentinel in text:
            return RoutingDecision(
                tier=RoutingTier.EXPORT_CONTROLLED,
                reason="sentinel present",
                classifier=self.classifier,
                matched_terms=[self.sentinel] if self.classifier == "keyword" else [],
            )
        return RoutingDecision(
            tier=RoutingTier.PUBLIC, reason="no sentinel", classifier=self.classifier
        )


#: A result the gate must withhold: content labelled export controlled at ingest.
def _ec(**fields):
    return {**fields, "classification": "export_controlled"}


def _run(coro):
    return asyncio.run(coro)


def _dispatcher_recording(counter: dict, result):
    async def _disp(name, args):
        counter["ran"] += 1
        return result

    return _disp


# --- Defect 1 / 2a: the sink gate classifies the result, not echoed arguments ---


def test_sink_gate_does_not_classify_echoed_arguments():
    """EC sentinel in the ARGUMENTS, benign result → allowed (the client sent it)."""
    counter = {"ran": 0}
    disp = _dispatcher_recording(counter, {"fragment_id": "f-1", "tool": "claude-code"})
    env = _run(
        R.gate_result_for_client(
            tool_name="axiom_memory__append",
            arguments={"summary": "ECSENTINEL billing note the client itself sent in"},
            dispatcher=disp,
            router=_FlagRouter(),
            client_ec_capable=False,
            client_name="claude-code",
        )
    )
    assert "result" in env, (
        "a benign ack was withheld because the client's own argument was reclassified"
    )
    assert counter["ran"] == 1


def test_sink_gate_still_withholds_ec_in_the_result():
    """An export-control label on the RESULT → withheld. The outbound guarantee is intact."""
    counter = {"ran": 0}
    disp = _dispatcher_recording(counter, _ec(payload="controlled code rode out in the result"))
    env = _run(
        R.gate_result_for_client(
            tool_name="some_read",
            arguments={"q": "benign"},
            dispatcher=disp,
            router=_FlagRouter(),
            client_ec_capable=False,
            client_name="claude-code",
        )
    )
    assert "result" not in env
    assert env["routing"]["refused"] is True


# --- Defect 2b: a committed mutation is never reported refused ---


def test_committed_mutation_is_not_reported_refused():
    counter = {"ran": 0}
    disp = _dispatcher_recording(counter, _ec(stored="x"))
    env = _run(
        R.gate_result_for_client(
            tool_name="axiom_memory__append",
            arguments={"summary": "x"},
            dispatcher=disp,
            router=_FlagRouter(),
            client_ec_capable=False,
            client_name="claude-code",
            tool_mutates=True,
        )
    )
    routing = env["routing"]
    assert counter["ran"] == 1, "the write must have run"
    assert "result" not in env, "the result bytes are still withheld from a non-EC client"
    assert routing["refused"] is False, "a committed write must not be reported refused"
    assert routing["committed"] is True
    assert routing["result_withheld"] is True


def test_advisory_model_guess_does_not_withhold(tmp_path, monkeypatch):
    """Regression guard for #1075 / ADR-152, now ADR-175, THROUGH this gate.

    Users saw a false "export-controlled" error for content that was not. Nothing
    here may quietly re-tighten that: an ``ollama`` EC verdict on a result must
    NOT withhold. Since ADR-175 the gate does not ask a model at all.
    """
    from axiom.infra import routing_audit

    monkeypatch.setattr(routing_audit, "_AUDIT_PATH", tmp_path / "routing_audit.jsonl")
    counter = {"ran": 0}
    disp = _dispatcher_recording(counter, {"payload": "ECSENTINEL only the small model flags"})
    env = _run(
        R.gate_result_for_client(
            tool_name="some_read",
            arguments={"q": "x"},
            dispatcher=disp,
            router=_FlagRouter(classifier="ollama"),
            client_ec_capable=False,
            client_name="claude-code",
        )
    )
    assert "result" in env, "an advisory model guess withheld a result — regression of #1075"
    assert env["result"] == {"payload": "ECSENTINEL only the small model flags"}


def test_read_withhold_is_unchanged():
    """A read whose result is labelled EC still refuses, with no committed flag."""
    counter = {"ran": 0}
    disp = _dispatcher_recording(counter, _ec(payload="x"))
    env = _run(
        R.gate_result_for_client(
            tool_name="some_read",
            arguments={"q": "x"},
            dispatcher=disp,
            router=_FlagRouter(),
            client_ec_capable=False,
            client_name="claude-code",
            tool_mutates=False,
        )
    )
    assert "result" not in env
    assert env["routing"]["refused"] is True
    assert env["routing"].get("committed") is False


def test_fail_closed_mutation_still_reports_committed(monkeypatch):
    """If the labels cannot be read, a mutation that already ran reports committed, not refused."""

    def _boom(result):
        raise RuntimeError("label scan failed")

    monkeypatch.setattr(R, "_label_state", _boom)
    counter = {"ran": 0}
    disp = _dispatcher_recording(counter, {"stored": "anything"})
    env = _run(
        R.gate_result_for_client(
            tool_name="axiom_memory__append",
            arguments={"summary": "x"},
            dispatcher=disp,
            router=_FlagRouter(),
            client_ec_capable=False,
            client_name="claude-code",
            tool_mutates=True,
        )
    )
    routing = env["routing"]
    assert counter["ran"] == 1
    assert routing["fail_safe"] is True
    assert routing["refused"] is False
    assert routing["committed"] is True


# --- Defect 3: every withhold is auditable by the id it returns ---


def test_every_withhold_is_logged_with_its_event_id(tmp_path, monkeypatch):
    from axiom.infra import routing_audit

    audit = tmp_path / "routing_audit.jsonl"
    monkeypatch.setattr(routing_audit, "_AUDIT_PATH", audit)

    counter = {"ran": 0}
    disp = _dispatcher_recording(counter, _ec(payload="x"))
    env = _run(
        R.gate_result_for_client(
            tool_name="some_read",
            arguments={"q": "x"},
            dispatcher=disp,
            router=_FlagRouter(),
            client_ec_capable=False,
            client_name="claude-code",
        )
    )
    event_id = env["routing"]["routing_event_id"]
    assert event_id, "a withhold must carry an id"
    found = routing_audit.find_routing_decision(event_id)
    assert found is not None, "the id the client was handed resolves to nothing — defect 3"
    assert found["routing_event_id"] == event_id
    assert found["tier"] == "export_controlled"


def test_audit_explain_resolves_a_routing_event_id(tmp_path, monkeypatch):
    from axiom.extensions.builtins.authz.skills import explain
    from axiom.infra import routing_audit

    audit = tmp_path / "routing_audit.jsonl"
    monkeypatch.setattr(routing_audit, "_AUDIT_PATH", audit)
    routing_audit.log_routing_decision(
        routing_event_id="rid-xyz",
        tier="export_controlled",
        classifier="keyword",
        reason="withheld: result classified export_controlled",
        matched_terms=["centrifuge"],
    )

    class _Ctx:
        pass

    # The skill looks the id up as a verdict first and falls back to the routing audit. Give it an
    # in-memory verdict store: with none injected it queried the real database, and the test passed
    # or failed on whether an earlier test in the same parallel worker had created that schema's
    # tables ("relation verdicts does not exist"), which kept CI red on main.
    import contextlib

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from axiom.extensions.builtins.authz.db_models import Base as AuthzBase

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    AuthzBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextlib.contextmanager
    def session_cm():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    res = explain.run({"receipt_id": "rid-xyz", "_session_cm": session_cm}, _Ctx())
    assert res.ok, f"audit explain could not resolve a routing id: {getattr(res, 'errors', None)}"
    blob = str(res.value)
    assert "rid-xyz" in blob and "export_controlled" in blob


# --- End-to-end: the server turns a committed-withhold into a non-error reply ---


def test_dispatch_call_reports_a_committed_mutation_not_an_error(tmp_path, monkeypatch):
    """Through the real ``dispatch_call``: a mutating tool whose result is EC
    comes back as committed, not as the ``error`` envelope the client saw."""
    import json
    from datetime import UTC, datetime

    from mcp.types import Tool, ToolAnnotations

    from axiom.extensions.builtins.mcp.aggregation import MCPSurface
    from axiom.extensions.builtins.mcp.server import dispatch_call
    from axiom.infra import routing_audit

    monkeypatch.setattr(routing_audit, "_AUDIT_PATH", tmp_path / "routing_audit.jsonl")
    monkeypatch.delenv("AXIOM_MCP_CLIENT_EC_CAPABLE", raising=False)  # non-EC client
    monkeypatch.delenv("AXIOM_MCP_CLIENT_PRINCIPAL", raising=False)  # local owner admitted

    ran = {"n": 0}

    async def _append(args):
        ran["n"] += 1
        return _ec(stored="a controlled-code chunk rode out in the write ack")

    surface = MCPSurface(
        tools=[
            Tool(
                name="axiom_memory__append",
                description="append",
                inputSchema={"type": "object"},
                annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
            )
        ],
        resources=[],
        prompts=[],
        dispatch={"axiom_memory__append": _append},
        content_hash="t",
        generated_at=datetime.now(UTC),
        sources=[],
        allowed_principals={"axiom_memory__append": ("@*:local",)},
    )

    content = _run(dispatch_call(surface, "axiom_memory__append", {"summary": "x"}))
    payload = json.loads(content[0].text)
    assert ran["n"] == 1, "the write ran"
    assert "error" not in payload, "a committed write was reported as an error"
    assert payload.get("committed") is True
    assert payload.get("result_withheld") is True
    assert payload["routing"]["refused"] is False


def test_a_routing_id_resolves_where_the_verdict_store_was_never_created(tmp_path, monkeypatch):
    """#1173: ``audit explain`` read the authorization ``verdicts`` table before
    the routing audit, so on a database where that table does not exist (a node
    that never migrated authz, or a test worker that ran no authz test first)
    a routing id could not be explained at all. The routing audit is a file and
    needs no database; it is consulted first."""
    from contextlib import contextmanager

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from axiom.extensions.builtins.authz.skills import explain
    from axiom.infra import routing_audit

    monkeypatch.setattr(routing_audit, "_AUDIT_PATH", tmp_path / "routing_audit.jsonl")
    routing_audit.log_routing_decision(
        routing_event_id="rid-no-store",
        tier="export_controlled",
        classifier="label",
        reason="withheld: stored label",
        matched_terms=[],
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")  # no tables at all

    @contextmanager
    def _empty_store():
        with Session(engine) as s:
            yield s

    res = explain.run({"receipt_id": "rid-no-store", "_session_cm": _empty_store}, object())
    assert res.ok, getattr(res, "errors", None)
    assert res.value["kind"] == "routing_decision"
