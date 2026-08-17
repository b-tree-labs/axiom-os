# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for the classification-aware MCP tool-routing layer.

Spec sketch:
  When a tool call arrives at the MCP dispatch path, consult the existing
  ``QueryRouter`` against the tool's free-text inputs. The classifier's
  ``RoutingDecision`` (public vs export_controlled) determines:

    1. Whether the call may dispatch to a remote peer at all, and which one.
    2. Whether an explicit ``peer=`` override is honored or refused.
    3. The ``routing`` block returned to the caller alongside the tool result.

  The routing layer NEVER touches the tool result payload itself — it only
  attaches a ``routing`` block, dispatches, and (in the EC-refusal case)
  short-circuits to a structured error.

Hard rules tested here:
  - public query  -> routes local; no remote dispatch unless explicitly overridden.
  - EC query      -> refuses any peer not flagged ``ec_eligible=True``.
  - explicit peer + public query -> honored, but the ``routing`` block records
    that the user supplied an override (so the audit trail can show it).
  - explicit peer + EC content + EC-eligible peer -> proceeds with reason
    "EC content + EC-eligible peer".
  - missing classifier (i.e. classifier raises) -> fail-safe: forced local,
    tier marked ``unknown`` with a typed reason.

The repo does not pull in ``pytest-asyncio``; tests drive the async API via
``asyncio.run``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from axiom.extensions.builtins.mcp.routing import (
    PeerDescriptor,
    PeerRegistry,
    RoutingProvenance,
    gate_result_for_client,
    route_tool_call,
    wrap_dispatcher,
)
from axiom.infra.router import (
    QueryRouter,
    RoutingDecision,
    RoutingTier,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _StubRouter:
    """Stand-in for QueryRouter — emits a fixed decision per call.

    Used in tests so we don't depend on Ollama or the keyword cache state.
    """

    decision: RoutingDecision | None = None
    raise_exc: BaseException | None = None
    calls: list[str] = field(default_factory=list)

    def classify(
        self,
        text: str,
        session_mode: str = "auto",
        context: list | None = None,
        sensitivity: str | None = None,
    ) -> RoutingDecision:
        self.calls.append(text)
        if self.raise_exc is not None:
            raise self.raise_exc
        assert self.decision is not None
        return self.decision


def _public_decision() -> RoutingDecision:
    return RoutingDecision(
        tier=RoutingTier.PUBLIC,
        reason="no export-control terms detected",
        classifier="fallback",
    )


def _ec_keyword_decision(term: str = "ITAR") -> RoutingDecision:
    return RoutingDecision(
        tier=RoutingTier.EXPORT_CONTROLLED,
        reason="export-control keyword match",
        matched_terms=[term],
        classifier="keyword",
        keyword_term=term,
    )


def _registry_with(*peers: PeerDescriptor) -> PeerRegistry:
    return PeerRegistry(peers=list(peers))


async def _ok_dispatcher(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """A minimal async tool handler that just echoes its inputs."""
    return {"tool": name, "args": arguments, "ran_on": "local"}


def _run(coro):  # tiny helper so each test reads cleanly
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Core routing decisions
# ---------------------------------------------------------------------------


def test_public_query_routes_local_no_remote_dispatch() -> None:
    router = _StubRouter(decision=_public_decision())
    registry = _registry_with(
        PeerDescriptor(name="host-a:ben", endpoint="https://host-a.local", ec_eligible=False),
    )

    result = _run(
        route_tool_call(
            tool_name="echo",
            arguments={"text": "what is 2+2?"},
            dispatcher=_ok_dispatcher,
            router=router,
            peers=registry,
        )
    )

    routing = result["routing"]
    assert routing["tier"] == "public"
    assert routing["forced_local"] is True
    assert routing["routed_to_peer"] is None
    assert "reason" in routing
    # Tool ran (we did not refuse a public query)
    assert result["result"]["ran_on"] == "local"


def test_export_controlled_query_refuses_public_only_peer() -> None:
    router = _StubRouter(decision=_ec_keyword_decision("ITAR"))
    registry = _registry_with(
        PeerDescriptor(
            name="portkey:openai-gpt5",
            endpoint="https://api.portkey.ai",
            ec_eligible=False,
        ),
    )

    result = _run(
        route_tool_call(
            tool_name="echo",
            arguments={"text": "discuss ITAR-controlled item"},
            dispatcher=_ok_dispatcher,
            router=router,
            peers=registry,
            requested_peer="portkey:openai-gpt5",
        )
    )

    routing = result["routing"]
    assert routing["tier"] == "export_controlled"
    # The dispatcher must NOT have run, and a refusal must be recorded.
    assert "result" not in result
    assert routing["refused"] is True
    assert routing["forced_local"] is True
    assert routing["routed_to_peer"] is None
    assert "ITAR" in routing["reason"] or "export-control" in routing["reason"]
    assert routing["refused_peer"] == "portkey:openai-gpt5"


def test_explicit_peer_with_public_query_honored_but_override_recorded() -> None:
    router = _StubRouter(decision=_public_decision())
    registry = _registry_with(
        PeerDescriptor(name="host-a:ben", endpoint="https://host-a.local", ec_eligible=True),
    )

    captured: dict[str, str] = {}

    async def remote_dispatcher(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        captured["name"] = name
        captured["peer"] = arguments.get("__peer__", "")
        return {"tool": name, "args": arguments, "ran_on": "host-a:ben"}

    result = _run(
        route_tool_call(
            tool_name="echo",
            arguments={"text": "what is the weather"},
            dispatcher=remote_dispatcher,
            router=router,
            peers=registry,
            requested_peer="host-a:ben",
        )
    )

    routing = result["routing"]
    assert routing["tier"] == "public"
    assert routing["routed_to_peer"] == "host-a:ben"
    assert routing["forced_local"] is False
    assert routing["override_honored"] is True
    # Override is loud in the reason so audit trails surface it.
    assert "override" in routing["reason"].lower()
    assert result["result"]["ran_on"] == "host-a:ben"


def test_explicit_peer_with_ec_content_and_ec_eligible_peer_proceeds() -> None:
    router = _StubRouter(decision=_ec_keyword_decision("ITAR"))
    registry = _registry_with(
        PeerDescriptor(name="host-a:ben", endpoint="https://host-a.local", ec_eligible=True),
    )

    async def remote_dispatcher(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"tool": name, "args": arguments, "ran_on": "host-a:ben"}

    result = _run(
        route_tool_call(
            tool_name="ec_compute",
            arguments={"text": "deemed export workflow"},
            dispatcher=remote_dispatcher,
            router=router,
            peers=registry,
            requested_peer="host-a:ben",
        )
    )

    routing = result["routing"]
    assert routing["tier"] == "export_controlled"
    assert routing["routed_to_peer"] == "host-a:ben"
    assert routing["forced_local"] is False
    # The reason must explicitly call out the EC-eligibility check.
    assert "EC content + EC-eligible peer" in routing["reason"]
    assert result["result"]["ran_on"] == "host-a:ben"


def test_missing_classifier_fails_safe_to_local() -> None:
    router = _StubRouter(raise_exc=RuntimeError("classifier crashed"))
    registry = _registry_with(
        PeerDescriptor(name="host-a:ben", endpoint="https://host-a.local", ec_eligible=True),
    )

    result = _run(
        route_tool_call(
            tool_name="echo",
            arguments={"text": "hello"},
            dispatcher=_ok_dispatcher,
            router=router,
            peers=registry,
        )
    )

    routing = result["routing"]
    # We must NEVER guess "public" when the classifier broke.
    assert routing["tier"] == "unknown"
    assert routing["forced_local"] is True
    assert routing["routed_to_peer"] is None
    assert routing["fail_safe"] is True
    assert "classifier" in routing["reason"].lower()
    # We still ran the tool — but locally only.
    assert result["result"]["ran_on"] == "local"


# ---------------------------------------------------------------------------
# Provenance helper — what gets persisted onto a memory fragment
# ---------------------------------------------------------------------------


def test_routing_provenance_round_trip_carries_classifier_reason() -> None:
    decision = _ec_keyword_decision("ITAR")
    prov = RoutingProvenance.from_decision(
        decision=decision,
        chosen_peer=None,
        forced_local=True,
        override_honored=False,
        refused=False,
        refused_peer=None,
        fail_safe=False,
    )
    payload = prov.to_dict()

    # Audit-relevant fields all present.
    assert payload["tier"] == "export_controlled"
    assert payload["classifier"] == "keyword"
    assert payload["matched_terms"] == ["ITAR"]
    assert payload["routing_event_id"] == decision.routing_event_id
    assert "reason" in payload
    assert payload["forced_local"] is True
    assert payload["chosen_peer"] is None


# ---------------------------------------------------------------------------
# wrap_dispatcher — drop-in wrapper for any async (name, args) -> result dispatch
# ---------------------------------------------------------------------------


def test_wrap_dispatcher_attaches_routing_block_to_existing_dispatcher() -> None:
    """The MCP server can wire the router with one line:

      ``wrapped = wrap_dispatcher(dispatch_call, router=..., peers=...)``
    """
    router = _StubRouter(decision=_public_decision())
    registry = _registry_with()

    async def underlying(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"value": arguments.get("text", "").upper()}

    wrapped = wrap_dispatcher(underlying, router=router, peers=registry)
    out = _run(wrapped("echo", {"text": "hello"}))

    assert out["result"] == {"value": "HELLO"}
    assert out["routing"]["tier"] == "public"
    # The wrapper extracted free-text args for classification.
    assert router.calls and "hello" in router.calls[0]


# ---------------------------------------------------------------------------
# Real classifier integration — exercise the actual QueryRouter once,
# proving we wire to the existing infra and don't re-implement classification.
# ---------------------------------------------------------------------------


def test_real_query_router_trips_on_default_ec_keyword() -> None:
    """Smoke test against the real ``QueryRouter`` with a default keyword.

    Uses an OllamaClassifier that always reports unavailable so the test
    is hermetic — keyword stage is enough.
    """
    from axiom.infra.router import OllamaClassifier

    class _UnreachableOllama(OllamaClassifier):
        def _check_available(self) -> bool:  # type: ignore[override]
            self._available = False
            return False

    router = QueryRouter(ollama=_UnreachableOllama())
    registry = _registry_with(
        PeerDescriptor(
            name="portkey:openai",
            endpoint="https://api.portkey.ai",
            ec_eligible=False,
        ),
    )

    # "ITAR" is in the built-in default term list.
    result = _run(
        route_tool_call(
            tool_name="echo",
            arguments={"text": "Please summarize the ITAR exemption process"},
            dispatcher=_ok_dispatcher,
            router=router,
            peers=registry,
            requested_peer="portkey:openai",
        )
    )
    assert result["routing"]["tier"] == "export_controlled"
    assert result["routing"]["refused"] is True
    assert "ITAR" in (result["routing"].get("matched_terms") or [])


# ---------------------------------------------------------------------------
# Client-sink gate (gate_result_for_client) — EC output never reaches a
# non-EC-capable client (e.g. an IDE whose model is a public cloud).
# ---------------------------------------------------------------------------


def test_client_gate_ec_capable_bypasses_and_returns_result():
    # EC-capable client: gate is a no-op; classifier never consulted.
    router = _StubRouter(raise_exc=AssertionError("must not classify"))
    result = _run(
        gate_result_for_client(
            tool_name="echo",
            arguments={"text": "anything"},
            dispatcher=_ok_dispatcher,
            router=router,
            client_ec_capable=True,
            client_name="claude-code",
        )
    )
    assert "result" in result and result["result"]["ran_on"] == "local"
    assert router.calls == []  # bypassed entirely


def test_client_gate_public_result_allowed_for_non_ec_client():
    router = _StubRouter(decision=_public_decision())
    result = _run(
        gate_result_for_client(
            tool_name="echo",
            arguments={"text": "the weather today"},
            dispatcher=_ok_dispatcher,
            router=router,
            client_ec_capable=False,
            client_name="cursor",
        )
    )
    assert "result" in result  # public content flows to non-EC client


def test_client_gate_withholds_ec_result_from_non_ec_client():
    router = _StubRouter(decision=_ec_keyword_decision("ITAR"))
    result = _run(
        gate_result_for_client(
            tool_name="echo",
            arguments={"text": "ITAR controlled deck"},
            dispatcher=_ok_dispatcher,
            router=router,
            client_ec_capable=False,
            client_name="cursor",
        )
    )
    assert "result" not in result  # WITHHELD — never reaches the client
    assert result["routing"]["refused"] is True
    assert result["routing"]["refused_peer"] == "cursor"
    assert result["routing"]["tier"] == "export_controlled"


def test_client_gate_classifies_the_RESULT_not_just_args():
    # Benign args, but the tool OUTPUT is EC — must still be withheld.
    async def _ec_output_dispatcher(name, arguments):
        return {"deck": "ITAR-controlled reactor core specification"}

    router = _StubRouter(decision=_ec_keyword_decision("ITAR"))
    result = _run(
        gate_result_for_client(
            tool_name="model_show",
            arguments={"id": "core-v3"},  # benign id
            dispatcher=_ec_output_dispatcher,
            router=router,
            client_ec_capable=False,
            client_name="cursor",
        )
    )
    assert "result" not in result
    assert result["routing"]["refused"] is True


def test_client_gate_fails_closed_when_classifier_breaks():
    router = _StubRouter(raise_exc=RuntimeError("ollama down"))
    result = _run(
        gate_result_for_client(
            tool_name="echo",
            arguments={"text": "uncertain"},
            dispatcher=_ok_dispatcher,
            router=router,
            client_ec_capable=False,
            client_name="cursor",
        )
    )
    assert "result" not in result  # fail CLOSED: withhold when unprovable
    assert result["routing"]["refused"] is True
    assert result["routing"]["fail_safe"] is True


# ---------------------------------------------------------------------------
# Interactive picker (non-TTY fallback is deterministic)
# ---------------------------------------------------------------------------


def test_picker_non_tty_returns_default(monkeypatch, capsys):
    from axiom.extensions.builtins.mcp._picker import select_index

    # stdin not a tty -> non-interactive: prints list, returns default.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    idx = select_index("Pick:", ["a", "b", "c"], default=1)
    assert idx == 1
    out = capsys.readouterr().out
    assert "1) a" in out and "2) b" in out


def test_picker_empty_options_returns_none():
    from axiom.extensions.builtins.mcp._picker import select_index

    assert select_index("Pick:", []) is None
