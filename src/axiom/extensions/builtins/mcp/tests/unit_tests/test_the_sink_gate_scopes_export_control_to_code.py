# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The client-sink gate says "export controlled" only about controlled code.

Export control attaches to a small, named set of controlled codes and methods.
Measurements, aggregates, table listings and coverage reports are the inputs
and outputs of work, not the codes, and are not export controlled. They may
still carry an access label for a different, lighter regime (proprietary,
personal, institutional-internal), which is not export control and must not be
reported as export control.

Pinned here (ADR-175):

1. Data-shaped results are released to a client whose model is not in-enclave,
   whatever a model or a keyword list would have said. This is the incident
   shape: a power aggregate, a latest reading, a table listing, a status report
   and an uncertainty-coverage report, every one withheld on a guess.
2. A result that merely NAMES a controlled code is released: the name is not
   export controlled; the code it contains is.
3. Negative controls: content labelled export controlled at ingest (the code
   itself) is still withheld, an access label still withholds under its own
   name, and an undeclared label still fails closed.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from axiom.extensions.builtins.mcp import routing as _routing
from axiom.extensions.builtins.mcp.routing import gate_result_for_client
from axiom.llm.router import RoutingDecision, RoutingTier


class _Router:
    """Stand-in for QueryRouter: one verdict for content without a listed term, and
    a control-list verdict for any chunk that contains a listed term."""

    def __init__(self, default: RoutingDecision, listed: tuple[str, ...] = ()) -> None:
        self.default = default
        self.listed = listed

    def classify(self, text: str, session_mode: str = "auto", context: Any = None, sensitivity: Any = None):
        hits = [t for t in self.listed if t.lower() in text.lower()]
        if hits:
            return RoutingDecision(
                tier=RoutingTier.EXPORT_CONTROLLED,
                reason="export-control keyword match",
                matched_terms=hits,
                classifier="keyword",
                keyword_term=hits[0],
            )
        if sensitivity == "permissive":  # the control list only; the model is skipped
            return RoutingDecision(tier=RoutingTier.PUBLIC, reason="no terms", classifier="fallback")
        return self.default


def _model_guesses_controlled() -> RoutingDecision:
    return RoutingDecision(
        tier=RoutingTier.EXPORT_CONTROLLED,
        reason="SLM: export-controlled content detected",
        classifier="ollama",
    )


def _model_says_public() -> RoutingDecision:
    return RoutingDecision(tier=RoutingTier.PUBLIC, reason="SLM: no", classifier="ollama")


def _gate(tool: str, payload: Any, router: Any) -> dict[str, Any]:
    async def _d(name: str, arguments: dict[str, Any]) -> Any:
        return payload

    return asyncio.run(
        gate_result_for_client(
            tool_name=tool, arguments={}, dispatcher=_d, router=router,
            client_ec_capable=False, client_name="claude-code",
        )
    )


@pytest.fixture(autouse=True)
def _default_mode(monkeypatch):
    monkeypatch.delenv(_routing.SINK_SEMANTIC_ENV, raising=False)


# Shapes of what a data surface returns. Names are generic on purpose: the gate
# must not depend on what a metric is called.
_DATA_RESULTS = {
    "aggregate": {"metric": "power", "count": 86400, "min": 0.0, "max": 950.2, "avg": 412.7, "unit": "kW"},
    "latest": {"metric": "power", "ts": "2026-10-05T23:59:59Z", "value": 0.0, "unit": "kW"},
    "table_listing": {"tables": [{"name": "readings_1s", "kind": "view"}, {"name": "runs", "kind": "table"}]},
    "daq_status": {"sources": [{"name": "daq-1", "state": "online", "last_seen": "2026-10-06T08:00:00Z"}]},
    "uncertainty_coverage": {"streams": [{"site": "site-a", "stream": "power", "exact": 0, "bounded": 0, "unclaimable": 86400}]},
}


@pytest.mark.parametrize("tool", sorted(_DATA_RESULTS))
def test_data_results_are_released_whatever_a_classifier_would_say(tool):
    out = _gate(tool, _DATA_RESULTS[tool], _Router(_model_guesses_controlled(), listed=("controlled-code-x",)))
    assert out.get("result") == _DATA_RESULTS[tool], out.get("routing")


def test_a_result_that_names_a_controlled_code_is_released():
    deck = {"deck": "controlled-code-x input: assembly map, cross-section library, method options"}
    out = _gate("model_show", deck, _Router(_model_says_public(), listed=("controlled-code-x",)))
    assert out.get("result") == deck, "the name is not export controlled; the code it contains is"


# --- negative controls: labelled controlled code is still withheld ----------


@pytest.mark.parametrize("label", [{"access_tier": "export_controlled"}, {"classification": "controlled"}])
def test_content_labelled_as_controlled_code_is_withheld_as_export_controlled(label):
    out = _gate("retrieve", {"results": [{"chunk_text": "x", **label}]}, _Router(_model_says_public()))
    assert "result" not in out
    assert out["routing"]["tier"] == "export_controlled"
    assert out["routing"]["refused"] is True


@pytest.mark.parametrize(
    "label",
    [{"classification": "regulated"}, {"classification": "cui"}, {"access_tier": "some-tier-nobody-declared"}],
)
def test_an_undeclared_label_fails_closed_and_is_not_called_export_control(label):
    out = _gate("retrieve", {"results": [{"chunk_text": "x", **label}]}, _Router(_model_says_public()))
    assert "result" not in out and out["routing"]["refused"] is True
    assert out["routing"]["tier"] == "unknown"


# --- the access-control regime: still withheld, but named for what it is ----


@pytest.mark.parametrize(
    "label",
    [{"access_tier": "restricted"}, {"classification": "internal"}, {"access_tier": "proprietary"}],
)
def test_an_access_label_withholds_but_is_not_reported_as_export_control(label):
    out = _gate("gold_series", {"rows": [{"value": 1.0, **label}]}, _Router(_model_guesses_controlled()))
    assert "result" not in out, "the access regime still applies; only its name changes"
    routing = out["routing"]
    assert routing["tier"] == "access_controlled"
    assert routing["classifier"] == "label"
    assert "not export control" in routing["reason"]


def test_a_code_name_inside_an_access_labelled_result_stays_an_access_withhold():
    payload = {"results": [{"access_tier": "restricted", "chunk_text": "controlled-code-x method notes"}]}
    out = _gate("retrieve", payload, _Router(_model_says_public(), listed=("controlled-code-x",)))
    assert "result" not in out
    assert out["routing"]["tier"] == "access_controlled"


def test_a_mixed_access_and_export_label_result_is_export_controlled():
    payload = {"results": [{"access_tier": "restricted"}, {"classification": "controlled"}]}
    out = _gate("retrieve", payload, _Router(_model_says_public()))
    assert out["routing"]["tier"] == "export_controlled"


# --- what the client is told -------------------------------------------------


def _wire(envelope_routing: dict[str, Any]) -> dict[str, Any]:
    from axiom.extensions.builtins.mcp import server

    return server._withheld_payload(envelope_routing)


def test_the_client_message_names_the_regime_that_withheld_it():
    ec = _wire({"tier": "export_controlled"})
    assert ec["error"].startswith("export-controlled content withheld")
    access = _wire({"tier": "access_controlled"})
    assert "export-controlled content" not in access["error"]
    assert "not export control" in access["error"]
    assert "access" in access["error"].lower()
    failed = _wire({"tier": "unknown", "fail_safe": True})
    assert "export-controlled content" not in failed["error"]
    for payload in (ec, access, failed):
        json.dumps(payload)
