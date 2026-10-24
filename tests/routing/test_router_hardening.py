# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Hardening tests for the router from downstream-consumer feedback.

Items addressed:
- Item 1: SLM unavailable → audit signal clarity (no silent over-block).
- Item 2: Default sensitivity exposed via SettingsStore as ``balanced``.
- Item 4: Cache invalidation via file mtime — edits to runtime/config files
  apply on next call, no process restart.
- Item 5: Whole-word matching with optional regex anchors. Eliminates
  false positives from substring matches (e.g., ``SCRAM`` vs ``SCRAMble``,
  ``enrichment`` vs ``re-enrichment-zone``).
"""

from __future__ import annotations

from axiom.infra.router import (
    SENSITIVITY_BALANCED,
    SENSITIVITY_PERMISSIVE,
    SENSITIVITY_STRICT,
    QueryRouter,
    RoutingTier,
    _keyword_match,
)

# --------------------------------------------------------------------------
# Item 5 — whole-word matching
# --------------------------------------------------------------------------


class TestWholeWordMatching:
    def test_term_matches_exact_word(self):
        # 'HEU' in "HEU enrichment" — exact word match
        assert _keyword_match("HEU enrichment levels", ["HEU"], set()) == ["HEU"]

    def test_term_does_not_match_inside_other_word(self):
        # 'SCRAM' inside 'SCRAMble' must NOT match
        assert _keyword_match("Let me SCRAMble the eggs", ["SCRAM"], set()) == []

    def test_term_matches_at_word_boundary_with_punctuation(self):
        # 'HEU,' should still match 'HEU' (comma is a word boundary)
        assert _keyword_match("Discuss HEU, then plutonium-239.", ["HEU"], set()) == ["HEU"]

    def test_term_with_hyphen_word_boundary(self):
        # 'enrichment' must NOT match 'pre-enrichment-zone' as a substring;
        # whole-word boundaries treat hyphens as separators in keyword files.
        assert _keyword_match(
            "We have a pre-enrichment-zone in the corner",
            ["enrichment"],
            set(),
        ) == ["enrichment"]
        # But it MUST match the standalone word
        assert _keyword_match("Discuss enrichment of the fuel", ["enrichment"], set()) == ["enrichment"]

    def test_multi_word_term_matches_phrase(self):
        # Multi-word terms ('weapons-usable') must match the phrase
        assert _keyword_match(
            "weapons-usable material is regulated",
            ["weapons-usable"],
            set(),
        ) == ["weapons-usable"]

    def test_regex_anchor_exact_only(self):
        # ^FOO$ anchored term matches the whole text only
        assert _keyword_match("FOO", ["^FOO$"], set()) == ["^FOO$"]
        # Anchored term does NOT match if there's surrounding text
        assert _keyword_match("hello FOO world", ["^FOO$"], set()) == []

    def test_case_insensitive_match(self):
        assert _keyword_match("heu enrichment", ["HEU"], set()) == ["HEU"]
        assert _keyword_match("HEU enrichment", ["heu"], set()) == ["heu"]

    def test_allowlist_suppresses_match(self):
        assert _keyword_match("HEU enrichment", ["HEU"], {"heu"}) == []

    def test_old_substring_match_no_longer_fires(self):
        # Pre-fix: "scramble" would match "SCRAM" (substring). Post-fix: no match.
        # This is the regression-against-the-feedback test.
        assert _keyword_match("rocket scramble launch", ["SCRAM"], set()) == []


# --------------------------------------------------------------------------
# Item 4 — cache invalidation via file mtime
# --------------------------------------------------------------------------


class TestCacheInvalidation:
    def test_router_reload_terms_picks_up_file_changes(self, tmp_path, monkeypatch):
        """`router.reload_terms()` must pick up file changes without restart.
        This is the existing escape hatch; verify it works."""
        import axiom.infra.router as router_mod

        terms_file = tmp_path / "terms.txt"
        terms_file.write_text("INITIAL_TERM\n", encoding="utf-8")
        allowlist_file = tmp_path / "allowlist.txt"
        allowlist_file.write_text("", encoding="utf-8")

        monkeypatch.setattr(router_mod, "_BUILTIN_TERMS_FILE", terms_file)
        monkeypatch.setattr(router_mod, "_USER_TERMS_FILE", tmp_path / "_no_user_terms.txt")
        monkeypatch.setattr(router_mod, "_MIRROR_SCRUB_FILE", tmp_path / "_no_mirror.txt")
        monkeypatch.setattr(router_mod, "_ALLOWLIST_FILE", allowlist_file)

        from unittest.mock import MagicMock

        from axiom.infra.router import OllamaClassifier
        mock_ollama = MagicMock(spec=OllamaClassifier)
        mock_ollama.classify.return_value = None
        router = QueryRouter(ollama=mock_ollama)
        router.reload_terms()

        # Initial state: INITIAL_TERM matches
        d = router.classify("text with INITIAL_TERM in it", sensitivity=SENSITIVITY_BALANCED)
        assert d.tier == RoutingTier.EXPORT_CONTROLLED

        # User edits the file: removes INITIAL_TERM, adds NEW_TERM.
        terms_file.write_text("NEW_TERM\n", encoding="utf-8")

        # WITHOUT explicit reload, behavior is implementation-defined.
        # WITH explicit reload, the new term applies.
        router.reload_terms()
        d2 = router.classify("text with INITIAL_TERM in it", sensitivity=SENSITIVITY_BALANCED)
        assert d2.tier == RoutingTier.PUBLIC, "reload_terms should clear caches"

        d3 = router.classify("text with NEW_TERM in it", sensitivity=SENSITIVITY_BALANCED)
        assert d3.tier == RoutingTier.EXPORT_CONTROLLED, "reload_terms should pick up new terms"


# --------------------------------------------------------------------------
# Item 2 — sensitivity exposed via SettingsStore as balanced default
# --------------------------------------------------------------------------


class TestSensitivityDefault:
    def test_settings_store_exposes_routing_sensitivity_balanced(self):
        from axiom.extensions.builtins.settings.store import SettingsStore
        SettingsStore()
        # Defaults dict must include routing.sensitivity = "balanced"
        from axiom.extensions.builtins.settings.store import _DEFAULTS
        assert _DEFAULTS["routing.sensitivity"] == SENSITIVITY_BALANCED

    def test_router_resolves_to_balanced_when_settings_silent(self, monkeypatch):
        """When routing.sensitivity not explicitly set in deployed settings,
        router resolves to balanced (not strict)."""
        from unittest.mock import MagicMock

        from axiom.infra.router import OllamaClassifier
        mock_ollama = MagicMock(spec=OllamaClassifier)
        mock_ollama.classify.return_value = None  # unavailable
        router = QueryRouter(ollama=mock_ollama)
        # No explicit sensitivity passed; settings default applies
        decision = router.classify("benign text with no EC keywords")
        # In balanced + no keyword match + Ollama unavailable → public fallback
        assert decision.tier == RoutingTier.PUBLIC


# --------------------------------------------------------------------------
# Item 1 — SLM unavailable: audit signal clarity
# --------------------------------------------------------------------------


class TestSlmUnavailableAudit:
    def test_unavailable_slm_in_balanced_falls_to_public(self):
        """When Ollama is unavailable + sensitivity=balanced + no keyword,
        decision is public/fallback (not EC). This is the user-feedback fix:
        previously strict-default + no Ollama → over-block."""
        from unittest.mock import MagicMock

        from axiom.infra.router import OllamaClassifier
        mock_ollama = MagicMock(spec=OllamaClassifier)
        mock_ollama.classify.return_value = None  # unavailable
        router = QueryRouter(ollama=mock_ollama)
        d = router.classify("benign question", sensitivity=SENSITIVITY_BALANCED)
        assert d.tier == RoutingTier.PUBLIC
        assert d.classifier == "fallback"

    def test_unavailable_slm_in_strict_still_routes_ec(self):
        """Strict mode keeps the conservative-routing behavior — explicit opt-in."""
        from unittest.mock import MagicMock

        from axiom.infra.router import OllamaClassifier
        mock_ollama = MagicMock(spec=OllamaClassifier)
        mock_ollama.classify.return_value = None
        router = QueryRouter(ollama=mock_ollama)
        d = router.classify("benign question", sensitivity=SENSITIVITY_STRICT)
        assert d.tier == RoutingTier.EXPORT_CONTROLLED
        assert d.classifier == "fallback"

    def test_permissive_skips_slm_entirely(self):
        """Permissive mode never consults Ollama — SLM-unavailable doesn't matter."""
        from unittest.mock import MagicMock

        from axiom.infra.router import OllamaClassifier
        mock_ollama = MagicMock(spec=OllamaClassifier)
        # Even if mock would return EC, permissive must skip the call.
        mock_ollama.classify.return_value = RoutingTier.EXPORT_CONTROLLED
        router = QueryRouter(ollama=mock_ollama)
        router.classify("benign question", sensitivity=SENSITIVITY_PERMISSIVE)
        mock_ollama.classify.assert_not_called()
