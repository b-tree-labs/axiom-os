# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for the label-aware MCP tool-routing layer.

Spec sketch (ADR-152, ADR-175, ADR-201 decisions 1, 4 and 16):
  When a tool call arrives at the MCP dispatch path, a LABEL decides where it
  may go. The request's declared labels (``access_tier`` / ``classification``)
  join to the strictest; an unlabelled request carries the site's default
  label, ``public`` unless the site declares otherwise. The detector
  (``QueryRouter.classify``) is consulted only where its answer could change
  the route, and its answer only ever narrows the route. It never refuses.

Hard rules tested here:
  - no peer named          -> local dispatch; the detector is not consulted.
  - labelled non-public    -> refused for any peer not flagged ``ec_eligible``,
                              by the label, with no model in the loop.
  - site default non-public-> an unlabelled request is refused the same way.
  - public + non-EC peer   -> honored, unless the detector advises export
                              control, in which case the call runs LOCALLY
                              (narrowed, not refused) and the advice is recorded.
  - public + EC-eligible peer -> honored; nothing to advise.
  - labelled + EC-eligible peer -> proceeds; the correct route, not an override.
  - detector raises        -> the peer option is removed, the request is served
                              locally, and the block says the advice was unavailable.

The repo does not pull in ``pytest-asyncio``; tests drive the async API via
``asyncio.run``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

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
    sensitivities: list = field(default_factory=list)

    def classify(
        self,
        text: str,
        session_mode: str = "auto",
        context: list | None = None,
        sensitivity: str | None = None,
    ) -> RoutingDecision:
        self.calls.append(text)
        self.sensitivities.append(sensitivity)
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


def _no_model() -> _StubRouter:
    """A detector that fails the test if it is consulted at all."""
    return _StubRouter(raise_exc=AssertionError("a model's answer is not an input here"))


def _registry_with(*peers: PeerDescriptor) -> PeerRegistry:
    return PeerRegistry(peers=list(peers))


_CLOUD = PeerDescriptor(name="portkey:openai-gpt5", endpoint="https://api.portkey.ai", ec_eligible=False)
_ENCLAVE = PeerDescriptor(name="host-a:ben", endpoint="https://host-a.local", ec_eligible=True)


async def _ok_dispatcher(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """A minimal async tool handler that just echoes its inputs."""
    return {"tool": name, "args": arguments, "ran_on": arguments.get("__peer__", "local")}


def _run(coro):  # tiny helper so each test reads cleanly
    return asyncio.run(coro)


def _route(arguments: dict[str, Any], *, router: Any, peers: PeerRegistry, peer: str | None = None, **kw: Any):
    return _run(
        route_tool_call(
            tool_name="echo",
            arguments=arguments,
            dispatcher=_ok_dispatcher,
            router=router,
            peers=peers,
            requested_peer=peer,
            **kw,
        )
    )


@pytest.fixture
def audit(tmp_path, monkeypatch):
    from axiom.infra import routing_audit

    path = tmp_path / "routing_audit.jsonl"
    monkeypatch.setattr(routing_audit, "_AUDIT_PATH", path)
    return routing_audit


# ---------------------------------------------------------------------------
# The verdict: a label, and nothing else
# ---------------------------------------------------------------------------


def test_public_query_routes_local_and_never_consults_the_detector() -> None:
    router = _no_model()
    result = _route({"text": "what is 2+2?"}, router=router, peers=_registry_with(_CLOUD))

    routing = result["routing"]
    assert routing["tier"] == "public"
    assert routing["classifier"] == "label"
    assert routing["forced_local"] is True
    assert routing["routed_to_peer"] is None
    assert routing["refused"] is False
    assert routing["labels"] == []
    assert "reason" in routing
    assert result["result"]["ran_on"] == "local"
    assert router.calls == [], "no peer named: nothing a detector says could change the route"


def test_a_labelled_request_is_refused_for_a_peer_that_is_not_ec_eligible(audit) -> None:
    router = _no_model()
    result = _route(
        {"text": "run the deck", "classification": "export_controlled"},
        router=router, peers=_registry_with(_CLOUD), peer=_CLOUD.name,
    )

    routing = result["routing"]
    assert "result" not in result, "the dispatcher must not have run"
    assert routing["refused"] is True
    assert routing["refused_peer"] == _CLOUD.name
    assert routing["forced_local"] is True
    assert routing["routed_to_peer"] is None
    assert routing["tier"] == "export_controlled"
    assert routing["classifier"] == "label"
    assert routing["labels"] == ["classification=export_controlled"]
    assert router.calls == [], "no model output is an input to the verdict (G2)"
    # Every refusal is on record by the id the caller is handed.
    assert routing["audit_recorded"] is True
    found = audit.find_routing_decision(routing["routing_event_id"])
    assert found is not None and found["tier"] == "export_controlled"
    assert found["labels"] == ["classification=export_controlled"]


def test_an_access_label_refuses_under_its_own_regime(audit) -> None:
    result = _route(
        {"query": "history", "access_tier": "restricted"},
        router=_no_model(), peers=_registry_with(_CLOUD), peer=_CLOUD.name,
    )
    routing = result["routing"]
    assert "result" not in result
    assert routing["tier"] == "access_controlled"
    assert "not export control" in routing["reason"]


def test_a_site_default_label_governs_an_unlabelled_request(audit) -> None:
    """ADR-201 decision 4: unlabelled is not unknown; it carries the site's default."""
    router = _no_model()
    result = _route(
        {"text": "anything at all"},
        router=router, peers=_registry_with(_CLOUD), peer=_CLOUD.name,
        default_label="export_controlled",
    )
    routing = result["routing"]
    assert "result" not in result
    assert routing["refused"] is True
    assert routing["tier"] == "export_controlled"
    assert routing["labels"] == ["site_default=export_controlled"]
    assert router.calls == []


def test_a_public_site_default_releases_an_unlabelled_request_to_a_peer() -> None:
    result = _route(
        {"text": "anything at all"},
        router=_StubRouter(decision=_public_decision()),
        peers=_registry_with(_CLOUD), peer=_CLOUD.name, default_label="public",
    )
    assert result["result"]["ran_on"] == _CLOUD.name


def test_labelled_content_and_ec_eligible_peer_proceeds_without_advice() -> None:
    router = _no_model()
    result = _route(
        {"text": "deemed export workflow", "classification": "export_controlled"},
        router=router, peers=_registry_with(_ENCLAVE), peer=_ENCLAVE.name,
    )
    routing = result["routing"]
    assert routing["tier"] == "export_controlled"
    assert routing["routed_to_peer"] == _ENCLAVE.name
    assert routing["forced_local"] is False
    assert routing["override_honored"] is False, "the correct route, not an override"
    assert "EC-eligible peer" in routing["reason"]
    assert result["result"]["ran_on"] == _ENCLAVE.name
    assert router.calls == []


# ---------------------------------------------------------------------------
# Advice: narrows a route, never refuses
# ---------------------------------------------------------------------------


def test_a_detector_verdict_cannot_refuse_it_narrows_the_route_to_local() -> None:
    """The detector says export control; the peer is a public-cloud relay.

    Before: refused on the verdict. Now: the request is served, locally, and
    the block says what advised and what that changed."""
    router = _StubRouter(decision=_ec_keyword_decision("ITAR"))
    result = _route(
        {"text": "discuss ITAR-controlled item"},
        router=router, peers=_registry_with(_CLOUD), peer=_CLOUD.name,
    )

    routing = result["routing"]
    assert result["result"]["ran_on"] == "local"
    assert "__peer__" not in result["result"]["args"]
    assert routing["refused"] is False
    assert routing["routed_to_peer"] is None
    assert routing["forced_local"] is True
    assert routing["override_honored"] is False
    assert routing["tier"] == "public", "the verdict is still the label's"
    assert routing["classifier"] == "label"
    assert routing["advice"]["effect"] == "rerouted_local"
    assert routing["advice"]["classifier"] == "keyword"
    assert routing["advice"]["matched_terms"] == ["ITAR"]
    assert "ITAR" in routing["reason"]
    assert router.calls == ["discuss ITAR-controlled item"]


def test_explicit_peer_with_public_query_honored_but_override_recorded() -> None:
    router = _StubRouter(decision=_public_decision())
    captured: dict[str, str] = {}

    async def remote_dispatcher(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        captured["name"] = name
        captured["peer"] = arguments.get("__peer__", "")
        return {"tool": name, "args": arguments, "ran_on": arguments.get("__peer__")}

    result = _run(
        route_tool_call(
            tool_name="echo",
            arguments={"text": "what is the weather"},
            dispatcher=remote_dispatcher,
            router=router,
            peers=_registry_with(_CLOUD),
            requested_peer=_CLOUD.name,
        )
    )

    routing = result["routing"]
    assert routing["tier"] == "public"
    assert routing["routed_to_peer"] == _CLOUD.name
    assert routing["forced_local"] is False
    assert routing["override_honored"] is True
    # Override is loud in the reason so audit trails surface it.
    assert "override" in routing["reason"].lower()
    assert routing["advice"]["effect"] == "none"
    assert result["result"]["ran_on"] == _CLOUD.name
    assert captured["peer"] == _CLOUD.name


def test_an_ec_eligible_peer_needs_no_advice() -> None:
    router = _no_model()
    result = _route({"text": "what is the weather"}, router=router, peers=_registry_with(_ENCLAVE), peer=_ENCLAVE.name)
    routing = result["routing"]
    assert routing["routed_to_peer"] == _ENCLAVE.name
    assert routing["override_honored"] is True
    assert routing["advice"] is None
    assert router.calls == []


def test_no_router_means_no_advice_and_the_peer_is_honoured() -> None:
    result = _route({"text": "hello"}, router=None, peers=_registry_with(_CLOUD), peer=_CLOUD.name)
    assert result["result"]["ran_on"] == _CLOUD.name
    assert result["routing"]["advice"] is None


def test_a_failed_detector_removes_the_peer_option_not_the_request() -> None:
    router = _StubRouter(raise_exc=RuntimeError("detector crashed"))
    result = _route({"text": "hello"}, router=router, peers=_registry_with(_CLOUD), peer=_CLOUD.name)

    routing = result["routing"]
    assert routing["tier"] == "public", "the verdict is the label's; the detector only advises"
    assert routing["fail_safe"] is True
    assert routing["forced_local"] is True
    assert routing["routed_to_peer"] is None
    assert routing["refused"] is False
    assert routing["advice"]["effect"] == "unavailable"
    assert "detector" in routing["reason"].lower()
    # We still ran the tool — but locally only.
    assert result["result"]["ran_on"] == "local"


def test_every_peer_route_names_the_server_revision() -> None:
    result = _route({"text": "hello"}, router=None, peers=_registry_with(_CLOUD))
    server = result["routing"]["server"]
    assert server is not None and server["revision"]
    assert server["source"].endswith("axiom")


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
    assert payload["labels"] == [] and payload["advice"] is None


# ---------------------------------------------------------------------------
# wrap_dispatcher — drop-in wrapper for any async (name, args) -> result dispatch
# ---------------------------------------------------------------------------


def test_wrap_dispatcher_attaches_routing_block_to_existing_dispatcher() -> None:
    """The MCP server can wire the router with one line:

      ``wrapped = wrap_dispatcher(dispatch_call, router=..., peers=...)``
    """
    router = _no_model()
    registry = _registry_with()

    async def underlying(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"value": arguments.get("text", "").upper()}

    wrapped = wrap_dispatcher(underlying, router=router, peers=registry)
    out = _run(wrapped("echo", {"text": "hello"}))

    assert out["result"] == {"value": "HELLO"}
    assert out["routing"]["tier"] == "public"
    assert out["routing"]["classifier"] == "label"
    assert router.calls == [], "no peer named: the detector is not consulted"


def test_wrap_dispatcher_carries_the_site_default_label(audit) -> None:
    wrapped = wrap_dispatcher(
        _ok_dispatcher, router=_no_model(), peers=_registry_with(_CLOUD), default_label="export_controlled"
    )
    out = _run(wrapped("echo", {"text": "hello", "__peer__": _CLOUD.name}))
    assert "result" not in out and out["routing"]["refused"] is True


# ---------------------------------------------------------------------------
# Real detector integration — exercise the actual QueryRouter once, proving we
# wire to the existing infra and don't re-implement classification.
# ---------------------------------------------------------------------------


def test_real_query_router_advice_on_a_default_ec_keyword_narrows_to_local() -> None:
    """Smoke test against the real ``QueryRouter`` with a default keyword.

    Uses an OllamaClassifier that always reports unavailable so the test
    is hermetic — keyword stage is enough. The keyword is advice: the call
    runs locally instead of at the cloud relay, and is not refused.
    """
    from axiom.infra.router import OllamaClassifier

    class _UnreachableOllama(OllamaClassifier):
        def _check_available(self) -> bool:  # type: ignore[override]
            self._available = False
            return False

    router = QueryRouter(ollama=_UnreachableOllama())
    result = _route(
        {"text": "Please summarize the ITAR exemption process"},
        router=router, peers=_registry_with(_CLOUD), peer=_CLOUD.name,
    )
    routing = result["routing"]
    assert result["result"]["ran_on"] == "local"
    assert routing["refused"] is False
    assert routing["tier"] == "public"
    assert routing["advice"]["effect"] == "rerouted_local"
    assert "ITAR" in (routing["advice"]["matched_terms"] or [])


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


# ---------------------------------------------------------------------------
# What may withhold a result: a stored label, and nothing else (ADR-175)
# ---------------------------------------------------------------------------
#
# Export control attaches to the controlled codes and their execution, not to a
# code's name or to data. The gate used to classify result TEXT (a keyword list of
# code names, then a small model) and withheld ordinary material that way. It now
# reads stored labels only, and every withhold names the regime that caused it.


def _gate(result_payload, router=None, client="claude-code", mutates=False):
    async def _d(name, arguments):
        return result_payload

    return _run(
        gate_result_for_client(
            tool_name="retrieve", arguments={"query": "history"}, dispatcher=_d,
            router=router, client_ec_capable=False, client_name=client,
            tool_mutates=mutates,
        )
    )


def test_a_result_naming_a_listed_code_is_released():
    # "the name isn't export controlled; the code it contains is"
    router = _StubRouter(decision=_ec_keyword_decision("ITAR"))
    out = _gate({"deck": "ITAR-listed code inputs and results"}, router)
    assert "result" in out and "routing" not in out
    assert router.calls == [], "result text is not classified at all"


def test_a_model_verdict_cannot_withhold(monkeypatch):
    sem = RoutingDecision(tier=RoutingTier.EXPORT_CONTROLLED, reason="model guess", classifier="ollama")
    monkeypatch.setenv("AXIOM_ROUTING_SINK_SEMANTIC", "enforce")  # retired switch: no effect
    assert "result" in _gate({"series": [1, 2, 3]}, _StubRouter(decision=sem))


def test_a_large_result_is_never_refused_for_its_size():
    payload = {"points": [{"ts": f"2026-01-01T00:{i % 60:02d}:00Z", "v": i * 0.5} for i in range(5000)]}
    assert "result" in _gate(payload, _StubRouter(raise_exc=RuntimeError("would fail if asked")))


@pytest.mark.parametrize("label", [{"classification": "export_controlled"}, {"access_tier": "export-controlled"}])
def test_content_labelled_export_controlled_is_withheld_as_export_control(label):
    out = _gate({"results": [{"chunk_text": "controlled source", **label}]})
    assert "result" not in out and out["routing"]["refused"] is True
    assert out["routing"]["tier"] == "export_controlled"
    assert out["routing"]["classifier"] == "label"


@pytest.mark.parametrize("value", ["restricted", "proprietary", "pii", "safeguards", "confidential"])
def test_an_access_label_is_withheld_and_reported_as_data_access(value):
    out = _gate({"results": [{"chunk_text": "x", "access_tier": value}]})
    assert "result" not in out and out["routing"]["tier"] == "access_controlled"
    assert "not export control" in out["routing"]["reason"]


def test_an_undeclared_label_fails_closed_without_calling_it_export_control():
    out = _gate({"results": [{"chunk_text": "x", "classification": "cui"}]})
    assert "result" not in out and out["routing"]["tier"] == "unknown"


def test_a_mixed_result_is_withheld_whole_under_the_strictest_regime():
    payload = {"results": [
        {"chunk_text": "a", "access_tier": "public"},
        {"chunk_text": "b", "access_tier": "proprietary"},
        {"chunk_text": "c", "classification": "export_controlled"},
    ]}
    out = _gate(payload)
    assert "result" not in out and out["routing"]["tier"] == "export_controlled"


def test_labels_are_found_in_nested_results():
    out = _gate({"a": {"b": [{"c": {"access_tier": "restricted"}}]}})
    assert "result" not in out


def test_public_labels_release():
    payload = {"results": [{"access_tier": "public", "classification": "unclassified", "chunk_text": "x"}]}
    assert "result" in _gate(payload)


def test_an_unreadable_label_scan_fails_closed(monkeypatch):
    from axiom.extensions.builtins.mcp import routing as _routing

    def _boom(result):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(_routing, "_label_state", _boom)
    out = _gate({"x": 1})
    assert "result" not in out and out["routing"]["fail_safe"] is True


def test_label_state_unit():
    from axiom.extensions.builtins.mcp.routing import _label_state

    assert _label_state({"x": 1}) == ("unlabeled", [])
    assert _label_state([{"access_tier": "public"}]) == ("public", [])
    assert _label_state([{"access_tier": "public"}, {"classification": "cui"}]) == ("nonpublic", ["classification=cui"])
