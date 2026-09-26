# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Routing weakness fix #3: EC-block error message must surface matched
keyword(s) and classifier name.

When an EC routing decision blocks a request (e.g. no EC provider configured,
EC content classified by a public-only deployment), the user-visible error
must answer two questions immediately, without forcing a follow-up
``axi log routing`` call:

  1. What term in my prompt triggered this?  (``RoutingDecision.matched_terms``)
  2. Which classifier stage made the call?    (``RoutingDecision.classifier``)

The shape of the message is:

    [EXPORT_CONTROLLED] Routed to private endpoint:
      matched 'HEU' via stage-1-keyword classifier.
    For details: axi log routing
    To allowlist: edit runtime/config/routing_allowlist.txt

This file pins the contract. It is domain-agnostic — no consumer-specific
or facility vocabulary. The placeholder term ``RESTRICTED-ALPHA`` is
a fictional token that stands in for any export-control keyword.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from axiom.infra.gateway import (
    Gateway,
    LLMProvider,
    _format_ec_block_message,
)
from axiom.infra.router import RoutingDecision, RoutingTier

# ---------------------------------------------------------------------------
# Helpers (mirror tests/routing/test_provider_routing_smoke.py)
# ---------------------------------------------------------------------------

def _make_provider(
    name: str,
    endpoint: str = "http://test.internal/v1",
    model: str = "test-model",
    priority: int = 50,
    routing_tier: str = "any",
    requires_vpn: bool = False,
    api_key: str = "test-key",
) -> LLMProvider:
    import os
    env_var = f"_AXIOM_EC_BLOCK_{name.upper().replace('-', '_')}"
    os.environ[env_var] = api_key
    return LLMProvider(
        name=name,
        endpoint=endpoint,
        model=model,
        priority=priority,
        routing_tier=routing_tier,
        requires_vpn=requires_vpn,
        api_key_env=env_var,
    )


@pytest.fixture()
def gateway_no_ec():
    """Gateway with one private-tier (any) provider and one public — no EC tier."""
    gw = Gateway.__new__(Gateway)
    gw._provider_override = None
    gw._model_override = None
    gw._ec_audit_enabled = False
    gw.providers = [
        _make_provider("private-llm", priority=10, routing_tier="any", requires_vpn=True),
        _make_provider("cloud-a",     priority=20, routing_tier="public"),
    ]
    return gw


# ---------------------------------------------------------------------------
# Pure helper: _format_ec_block_message
# ---------------------------------------------------------------------------

class TestFormatEcBlockMessage:
    """The formatter is the single source of truth for the user-visible string."""

    def test_message_includes_export_controlled_prefix(self):
        decision = RoutingDecision(
            tier=RoutingTier.EXPORT_CONTROLLED,
            reason="export-control keyword match",
            matched_terms=["RESTRICTED-ALPHA"],
            classifier="keyword",
            keyword_term="RESTRICTED-ALPHA",
        )
        msg = _format_ec_block_message(decision)
        assert "[EXPORT_CONTROLLED]" in msg

    def test_message_includes_matched_term(self):
        decision = RoutingDecision(
            tier=RoutingTier.EXPORT_CONTROLLED,
            reason="export-control keyword match",
            matched_terms=["RESTRICTED-ALPHA"],
            classifier="keyword",
            keyword_term="RESTRICTED-ALPHA",
        )
        msg = _format_ec_block_message(decision)
        assert "RESTRICTED-ALPHA" in msg
        assert "matched" in msg.lower()

    def test_message_includes_stage_label_for_keyword_classifier(self):
        decision = RoutingDecision(
            tier=RoutingTier.EXPORT_CONTROLLED,
            reason="export-control keyword match",
            matched_terms=["RESTRICTED-ALPHA"],
            classifier="keyword",
        )
        msg = _format_ec_block_message(decision)
        assert "stage-1-keyword" in msg

    def test_message_includes_stage_label_for_ollama_classifier(self):
        decision = RoutingDecision(
            tier=RoutingTier.EXPORT_CONTROLLED,
            reason="SLM: export-controlled content detected",
            matched_terms=[],
            classifier="ollama",
        )
        msg = _format_ec_block_message(decision)
        assert "stage-2-ollama" in msg

    def test_message_includes_stage_label_for_fallback_classifier(self):
        decision = RoutingDecision(
            tier=RoutingTier.EXPORT_CONTROLLED,
            reason="strict mode default",
            matched_terms=[],
            classifier="fallback",
        )
        msg = _format_ec_block_message(decision)
        assert "stage-3-fallback" in msg

    def test_message_lists_multiple_matched_terms(self):
        decision = RoutingDecision(
            tier=RoutingTier.EXPORT_CONTROLLED,
            reason="export-control keyword match",
            matched_terms=["RESTRICTED-ALPHA", "RESTRICTED-BETA"],
            classifier="keyword",
            keyword_term="RESTRICTED-ALPHA",
        )
        msg = _format_ec_block_message(decision)
        assert "RESTRICTED-ALPHA" in msg
        assert "RESTRICTED-BETA" in msg

    def test_message_points_to_log_command(self):
        decision = RoutingDecision(
            tier=RoutingTier.EXPORT_CONTROLLED,
            reason="export-control keyword match",
            matched_terms=["RESTRICTED-ALPHA"],
            classifier="keyword",
        )
        msg = _format_ec_block_message(decision)
        assert "axi log routing" in msg

    def test_message_points_to_allowlist(self):
        decision = RoutingDecision(
            tier=RoutingTier.EXPORT_CONTROLLED,
            reason="export-control keyword match",
            matched_terms=["RESTRICTED-ALPHA"],
            classifier="keyword",
        )
        msg = _format_ec_block_message(decision)
        assert "routing_allowlist.txt" in msg

    def test_message_handles_no_decision_gracefully(self):
        """When called without a decision, formatter returns the legacy
        ``axi log routing`` guidance — no crash, signal preserved."""
        msg = _format_ec_block_message(None)
        assert "[EXPORT_CONTROLLED]" in msg
        assert "axi log routing" in msg

    def test_message_handles_decision_with_no_matched_terms(self):
        """Ollama / fallback decisions have no matched terms — message must
        still surface the classifier stage."""
        decision = RoutingDecision(
            tier=RoutingTier.EXPORT_CONTROLLED,
            reason="SLM: export-controlled content detected",
            matched_terms=[],
            classifier="ollama",
        )
        msg = _format_ec_block_message(decision)
        assert "[EXPORT_CONTROLLED]" in msg
        assert "stage-2-ollama" in msg

    def test_message_caps_term_list_length(self):
        """Many matched terms → only first few shown verbatim, summary tail."""
        terms = [f"TERM-{i}" for i in range(20)]
        decision = RoutingDecision(
            tier=RoutingTier.EXPORT_CONTROLLED,
            reason="export-control keyword match",
            matched_terms=terms,
            classifier="keyword",
        )
        msg = _format_ec_block_message(decision)
        # First few terms are present
        assert "TERM-0" in msg
        # The full set isn't pasted verbatim — output is bounded
        assert "TERM-19" not in msg or "more" in msg


# ---------------------------------------------------------------------------
# End-to-end: gateway plumbs RoutingDecision into the EC-block response
# ---------------------------------------------------------------------------

class TestGatewayECBlockSurfacesDecision:

    def test_ec_block_text_includes_matched_term_when_decision_passed(self, gateway_no_ec):
        decision = RoutingDecision(
            tier=RoutingTier.EXPORT_CONTROLLED,
            reason="export-control keyword match",
            matched_terms=["RESTRICTED-ALPHA"],
            classifier="keyword",
            keyword_term="RESTRICTED-ALPHA",
        )
        with patch.object(gateway_no_ec, "_check_vpn", return_value=True):
            result = gateway_no_ec.complete_with_tools(
                messages=[{"role": "user", "content": "Test prompt."}],
                routing_tier="export_controlled",
                routing_decision=decision,
            )
        assert result.success is False
        assert result.error == "EC_PROVIDER_NOT_CONFIGURED"
        assert "RESTRICTED-ALPHA" in result.text
        assert "stage-1-keyword" in result.text

    def test_ec_block_text_includes_ollama_classifier_label(self, gateway_no_ec):
        decision = RoutingDecision(
            tier=RoutingTier.EXPORT_CONTROLLED,
            reason="SLM: export-controlled content detected",
            matched_terms=[],
            classifier="ollama",
        )
        with patch.object(gateway_no_ec, "_check_vpn", return_value=True):
            result = gateway_no_ec.complete_with_tools(
                messages=[{"role": "user", "content": "Test prompt."}],
                routing_tier="export_controlled",
                routing_decision=decision,
            )
        assert result.success is False
        assert result.error == "EC_PROVIDER_NOT_CONFIGURED"
        assert "stage-2-ollama" in result.text

    def test_ec_block_text_works_without_decision(self, gateway_no_ec):
        """Backward-compat: callers that don't pass routing_decision still get
        a usable message — the original signal (warning prefix + tier) stays."""
        with patch.object(gateway_no_ec, "_check_vpn", return_value=True):
            result = gateway_no_ec.complete_with_tools(
                messages=[{"role": "user", "content": "Test prompt."}],
                routing_tier="export_controlled",
            )
        assert result.success is False
        assert result.error == "EC_PROVIDER_NOT_CONFIGURED"
        assert "export-controlled" in result.text.lower()

    def test_ec_block_message_is_domain_agnostic(self, gateway_no_ec):
        """Per CLAUDE.md: axiom docs and user-facing strings never name domain
        consumers. Verify no domain-specific consumer vocabulary leaks in."""
        decision = RoutingDecision(
            tier=RoutingTier.EXPORT_CONTROLLED,
            reason="export-control keyword match",
            matched_terms=["GENERIC-TERM"],
            classifier="keyword",
        )
        with patch.object(gateway_no_ec, "_check_vpn", return_value=True):
            result = gateway_no_ec.complete_with_tools(
                messages=[{"role": "user", "content": "Test prompt."}],
                routing_tier="export_controlled",
                routing_decision=decision,
            )
        text_lower = result.text.lower()
        for forbidden in ("nuclear", "reactor", "netl", "rascal", "facility-specific"):
            assert forbidden not in text_lower, (
                f"EC block message must be domain-agnostic; found {forbidden!r}"
            )


# ---------------------------------------------------------------------------
# Regression: original signal (prefix + tier) is preserved
# ---------------------------------------------------------------------------

class TestECBlockShapeBackwardCompat:
    """The *shape* may change but the *signal* must stay (per task spec):

      - "[EXPORT_CONTROLLED]" prefix in the message
      - Mention of export-controlled tier / private-network routing
      - A pointer to where to learn more (axi log routing)
    """

    def test_signal_warning_prefix_preserved(self, gateway_no_ec):
        decision = RoutingDecision(
            tier=RoutingTier.EXPORT_CONTROLLED,
            reason="export-control keyword match",
            matched_terms=["RESTRICTED-ALPHA"],
            classifier="keyword",
        )
        with patch.object(gateway_no_ec, "_check_vpn", return_value=True):
            result = gateway_no_ec.complete_with_tools(
                messages=[{"role": "user", "content": "Test prompt."}],
                routing_tier="export_controlled",
                routing_decision=decision,
            )
        assert "[EXPORT_CONTROLLED]" in result.text

    def test_signal_tier_label_preserved(self, gateway_no_ec):
        decision = RoutingDecision(
            tier=RoutingTier.EXPORT_CONTROLLED,
            reason="export-control keyword match",
            matched_terms=["RESTRICTED-ALPHA"],
            classifier="keyword",
        )
        with patch.object(gateway_no_ec, "_check_vpn", return_value=True):
            result = gateway_no_ec.complete_with_tools(
                messages=[{"role": "user", "content": "Test prompt."}],
                routing_tier="export_controlled",
                routing_decision=decision,
            )
        # Either the tier label or the export-controlled phrase must remain
        assert "export-controlled" in result.text.lower() or "EXPORT_CONTROLLED" in result.text
