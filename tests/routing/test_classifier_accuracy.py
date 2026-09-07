# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Red-team classifier accuracy tests.

These tests enforce that the QueryRouter correctly classifies known prompts.
They are the primary mechanism for tracking false positives and false negatives.

To add a regression test:
  - False negative (bypass that should be caught): add to export_controlled_prompts.txt
  - False positive (over-triggering): add to public_prompts.txt, adjust allowlist if needed

Sensitivity sweep:
  - strict:     export_controlled prompts must always be caught (no false negatives)
  - balanced:   default behavior — both sets should pass
  - permissive: public prompts must never be over-flagged (no false positives from keywords)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from axiom.infra.router import (
    SENSITIVITY_BALANCED,
    SENSITIVITY_PERMISSIVE,
    SENSITIVITY_STRICT,
    OllamaClassifier,
    QueryRouter,
    RoutingTier,
)

_TESTS_DIR = Path(__file__).parent


def _load_prompts(filename: str) -> list[str]:
    path = _TESTS_DIR / filename
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def _router_no_ollama(sensitivity: str = SENSITIVITY_BALANCED) -> QueryRouter:
    """Router with Ollama disabled so tests run offline and deterministically."""
    mock_ollama = MagicMock(spec=OllamaClassifier)
    mock_ollama.classify.return_value = None  # simulate unavailable
    router = QueryRouter(ollama=mock_ollama)
    return router


# ---------------------------------------------------------------------------
# Export-controlled prompts — must never reach cloud
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prompt", _load_prompts("export_controlled_prompts.txt"))
def test_export_controlled_prompt_balanced(prompt: str) -> None:
    """Balanced sensitivity: keyword-matched export-controlled prompts must be caught."""
    router = _router_no_ollama(SENSITIVITY_BALANCED)
    decision = router.classify(prompt, sensitivity=SENSITIVITY_BALANCED)
    assert decision.tier == RoutingTier.EXPORT_CONTROLLED, (
        f"False negative [{SENSITIVITY_BALANCED}]: routed to public\n  prompt: {prompt!r}\n  decision: {decision}"
    )


@pytest.mark.parametrize("prompt", _load_prompts("export_controlled_prompts.txt"))
def test_export_controlled_prompt_strict(prompt: str) -> None:
    """Strict sensitivity: no false negatives allowed under any circumstances."""
    router = _router_no_ollama(SENSITIVITY_STRICT)
    decision = router.classify(prompt, sensitivity=SENSITIVITY_STRICT)
    assert decision.tier == RoutingTier.EXPORT_CONTROLLED, (
        f"False negative [{SENSITIVITY_STRICT}]: routed to public\n  prompt: {prompt!r}\n  decision: {decision}"
    )


@pytest.mark.parametrize("prompt", _load_prompts("export_controlled_prompts.txt"))
def test_export_controlled_prompt_permissive(prompt: str) -> None:
    """Permissive sensitivity: keyword matches are still authoritative."""
    router = _router_no_ollama(SENSITIVITY_PERMISSIVE)
    decision = router.classify(prompt, sensitivity=SENSITIVITY_PERMISSIVE)
    assert decision.tier == RoutingTier.EXPORT_CONTROLLED, (
        f"False negative [{SENSITIVITY_PERMISSIVE}]: routed to public\n  prompt: {prompt!r}\n  decision: {decision}"
    )


# ---------------------------------------------------------------------------
# Public prompts — must not be over-flagged as export_controlled
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prompt", _load_prompts("public_prompts.txt"))
def test_public_prompt_balanced(prompt: str) -> None:
    """Balanced sensitivity: safe prompts must route to cloud."""
    router = _router_no_ollama(SENSITIVITY_BALANCED)
    decision = router.classify(prompt, sensitivity=SENSITIVITY_BALANCED)
    assert decision.tier == RoutingTier.PUBLIC, (
        f"False positive [{SENSITIVITY_BALANCED}]: routed to export_controlled\n  prompt: {prompt!r}\n  decision: {decision}"
    )


@pytest.mark.parametrize("prompt", _load_prompts("public_prompts.txt"))
def test_public_prompt_permissive(prompt: str) -> None:
    """Permissive sensitivity: safe prompts must route to cloud."""
    router = _router_no_ollama(SENSITIVITY_PERMISSIVE)
    decision = router.classify(prompt, sensitivity=SENSITIVITY_PERMISSIVE)
    assert decision.tier == RoutingTier.PUBLIC, (
        f"False positive [{SENSITIVITY_PERMISSIVE}]: routed to export_controlled\n  prompt: {prompt!r}\n  decision: {decision}"
    )


# ---------------------------------------------------------------------------
# Context window regression tests
# ---------------------------------------------------------------------------

def test_context_window_catches_topic_drift() -> None:
    """A follow-up with no keywords is still export_controlled when prior context has them."""
    router = _router_no_ollama()
    # Use a current-list EC term (HEU). The mechanism under test is context-window
    # detection, not the specific term. See _export_control_terms_default.txt.
    ctx = [{"role": "user", "content": "what enrichment level qualifies as HEU?"}]
    decision = router.classify("What's the default source definition syntax?", context=ctx)
    assert decision.tier == RoutingTier.EXPORT_CONTROLLED
    assert decision.classifier == "keyword"


def test_clean_context_does_not_escalate() -> None:
    """Benign context must not escalate a benign current message."""
    router = _router_no_ollama()
    ctx = [{"role": "user", "content": "how do I write a pytest fixture?"}]
    decision = router.classify("Can you explain async/await?", context=ctx)
    assert decision.tier == RoutingTier.PUBLIC


# ---------------------------------------------------------------------------
# Sensitivity behavior tests
# ---------------------------------------------------------------------------

def test_strict_fallback_is_export_controlled() -> None:
    """In strict mode, when Ollama is unavailable and no keywords match, route conservatively."""
    router = _router_no_ollama()
    decision = router.classify("Tell me about system analysis", sensitivity=SENSITIVITY_STRICT)
    assert decision.tier == RoutingTier.EXPORT_CONTROLLED
    assert decision.classifier == "fallback"


def test_balanced_fallback_is_public() -> None:
    router = _router_no_ollama()
    decision = router.classify("Tell me about system analysis", sensitivity=SENSITIVITY_BALANCED)
    assert decision.tier == RoutingTier.PUBLIC
    assert decision.classifier == "fallback"


def test_permissive_skips_ollama() -> None:
    """Permissive mode must not call Ollama at all."""
    mock_ollama = MagicMock(spec=OllamaClassifier)
    mock_ollama.classify.return_value = RoutingTier.EXPORT_CONTROLLED  # would trigger if called
    router = QueryRouter(ollama=mock_ollama)
    decision = router.classify("How do I write a Python test?", sensitivity=SENSITIVITY_PERMISSIVE)
    mock_ollama.classify.assert_not_called()
    assert decision.tier == RoutingTier.PUBLIC


def test_ollama_uncertain_strict_routes_ec() -> None:
    """In strict mode, Ollama 'uncertain' → export_controlled."""
    mock_ollama = MagicMock(spec=OllamaClassifier)
    mock_ollama._check_available.return_value = True
    mock_ollama.classify.return_value = "uncertain"
    router = QueryRouter(ollama=mock_ollama)
    decision = router.classify("Tell me about system analysis", sensitivity=SENSITIVITY_STRICT)
    assert decision.tier == RoutingTier.EXPORT_CONTROLLED
    assert decision.classifier == "ollama"


def test_ollama_uncertain_balanced_falls_through() -> None:
    """In balanced mode, Ollama 'uncertain' falls through to public fallback."""
    mock_ollama = MagicMock(spec=OllamaClassifier)
    mock_ollama._check_available.return_value = True
    mock_ollama.classify.return_value = "uncertain"
    router = QueryRouter(ollama=mock_ollama)
    decision = router.classify("Tell me about system analysis", sensitivity=SENSITIVITY_BALANCED)
    assert decision.tier == RoutingTier.PUBLIC
    assert decision.classifier == "fallback"


# ---------------------------------------------------------------------------
# Allowlist tests
# ---------------------------------------------------------------------------

def test_allowlist_suppresses_false_positive(tmp_path, monkeypatch) -> None:
    """Terms in the allowlist must not trigger export_controlled even if in keyword list."""
    import axiom.infra.router as router_mod

    allowlist = tmp_path / "routing_allowlist.txt"
    allowlist.write_text("InternalSim\n", encoding="utf-8")

    monkeypatch.setattr(router_mod, "_ALLOWLIST_FILE", allowlist)

    router = _router_no_ollama()
    router.reload_terms()  # clear caches so monkeypatch takes effect
    decision = router.classify("How do I model geometry in InternalSim?", sensitivity=SENSITIVITY_BALANCED)
    # With InternalSim on the allowlist it should fall through (balanced → public fallback)
    assert decision.tier == RoutingTier.PUBLIC


def test_allowlist_does_not_suppress_other_terms(tmp_path, monkeypatch) -> None:
    """Allowlisting one term must not affect unrelated export-controlled terms."""
    import axiom.infra.router as router_mod

    allowlist = tmp_path / "routing_allowlist.txt"
    # Use current-list EC term so the allowlist actually has effect.
    allowlist.write_text("HEU\n", encoding="utf-8")

    monkeypatch.setattr(router_mod, "_ALLOWLIST_FILE", allowlist)

    router = _router_no_ollama()
    router.reload_terms()
    # weapons-usable is not allowlisted — must still trigger
    decision = router.classify("Help calculate weapons-usable material thresholds")
    assert decision.tier == RoutingTier.EXPORT_CONTROLLED
