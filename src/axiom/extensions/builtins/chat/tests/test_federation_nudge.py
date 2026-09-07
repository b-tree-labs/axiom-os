# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the chat-time federation re-probe nudge.

Closes the last gap in the "neut chat finds a self-hosted node and suggests it"
vision (PR #224, #227, #212 cover the install-time + zero-config-LLM
halves; this is the chat-startup nudge that prompts the operator
when a richer remote provider is reachable but hasn't been adopted).

Hooks into the chat-startup path pre-REPL. Cheap, non-blocking, TTY-
only, respects the decline memo from PR #227 so the prompt doesn't
nag operators who already said no at install time.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _mk_probe_result(*, name="qwen-example", reachable=True,
                     endpoint="https://example.local/v1"):
    """Build a ProbeResult-shaped object the tests can hand to the
    nudge module without instantiating the real Connection."""
    from axiom.setup.federation_probe import ProbeResult
    conn = MagicMock()
    conn.name = name
    conn.endpoint = endpoint
    conn.display_name = name
    return ProbeResult(
        connection=conn, reachable=reachable, latency_ms=120,
        rag_corpus=None,
    )


# ---------------------------------------------------------------------------
# Happy path — one reachable, not declined, not adopted → nudge prints
# ---------------------------------------------------------------------------


class TestNudgePrints:
    def test_one_reachable_unadopted_undeclined_prints_tip(
        self, capsys, monkeypatch,
    ):
        from axiom.extensions.builtins.chat import federation_nudge

        monkeypatch.setattr(federation_nudge, "_is_tty", lambda: True)
        monkeypatch.setattr(
            federation_nudge, "_discover", lambda: [_mk_probe_result()],
        )
        monkeypatch.setattr(
            federation_nudge, "_already_adopted", lambda name: False,
        )
        monkeypatch.setattr(
            federation_nudge, "_has_declined", lambda name: False,
        )

        federation_nudge.maybe_render_federation_nudge()
        out = capsys.readouterr().out
        assert "qwen-example" in out
        assert "reachable" in out.lower() or "available" in out.lower()
        # Includes an adopt command so the operator knows what to do
        assert "federation discover" in out or "chat --use" in out


# ---------------------------------------------------------------------------
# Suppression — every "don't bother the operator" branch
# ---------------------------------------------------------------------------


class TestNudgeSuppression:
    def test_silent_when_not_a_tty(self, capsys, monkeypatch):
        """CI / piped output / non-interactive context → never print."""
        from axiom.extensions.builtins.chat import federation_nudge

        monkeypatch.setattr(federation_nudge, "_is_tty", lambda: False)
        monkeypatch.setattr(
            federation_nudge, "_discover",
            lambda: (_ for _ in ()).throw(AssertionError("should not probe in non-TTY")),
        )
        federation_nudge.maybe_render_federation_nudge()
        assert capsys.readouterr().out == ""

    def test_silent_when_no_reachable_candidates(
        self, capsys, monkeypatch,
    ):
        from axiom.extensions.builtins.chat import federation_nudge

        monkeypatch.setattr(federation_nudge, "_is_tty", lambda: True)
        monkeypatch.setattr(federation_nudge, "_discover", lambda: [])

        federation_nudge.maybe_render_federation_nudge()
        assert capsys.readouterr().out == ""

    def test_silent_when_candidate_already_adopted(
        self, capsys, monkeypatch,
    ):
        """If the operator already adopted this provider in
        llm-providers.toml, the nudge is noise."""
        from axiom.extensions.builtins.chat import federation_nudge

        monkeypatch.setattr(federation_nudge, "_is_tty", lambda: True)
        monkeypatch.setattr(
            federation_nudge, "_discover", lambda: [_mk_probe_result()],
        )
        monkeypatch.setattr(
            federation_nudge, "_already_adopted", lambda name: True,
        )
        monkeypatch.setattr(
            federation_nudge, "_has_declined", lambda name: False,
        )
        federation_nudge.maybe_render_federation_nudge()
        assert capsys.readouterr().out == ""

    def test_silent_when_candidate_was_declined(
        self, capsys, monkeypatch,
    ):
        """Respects the install-time decline memo (PR #227)."""
        from axiom.extensions.builtins.chat import federation_nudge

        monkeypatch.setattr(federation_nudge, "_is_tty", lambda: True)
        monkeypatch.setattr(
            federation_nudge, "_discover", lambda: [_mk_probe_result()],
        )
        monkeypatch.setattr(
            federation_nudge, "_already_adopted", lambda name: False,
        )
        monkeypatch.setattr(
            federation_nudge, "_has_declined", lambda name: True,
        )
        federation_nudge.maybe_render_federation_nudge()
        assert capsys.readouterr().out == ""

    def test_silent_when_discover_raises(self, capsys, monkeypatch):
        """Federation unreachable / permission denied / any probe
        failure → silent. Chat startup must NEVER block or fail
        because of the nudge."""
        from axiom.extensions.builtins.chat import federation_nudge

        monkeypatch.setattr(federation_nudge, "_is_tty", lambda: True)
        monkeypatch.setattr(
            federation_nudge, "_discover",
            lambda: (_ for _ in ()).throw(RuntimeError("network unreachable")),
        )
        # Must not raise
        federation_nudge.maybe_render_federation_nudge()
        assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# Multiple candidates — show the first one (don't spam the welcome banner)
# ---------------------------------------------------------------------------


class TestMultipleCandidates:
    def test_one_tip_even_when_many_candidates(
        self, capsys, monkeypatch,
    ):
        """Don't print 5 nudges. The first reachable+unadopted
        candidate is enough to prompt the operator."""
        from axiom.extensions.builtins.chat import federation_nudge

        monkeypatch.setattr(federation_nudge, "_is_tty", lambda: True)
        monkeypatch.setattr(
            federation_nudge, "_discover", lambda: [
                _mk_probe_result(name="provider-a"),
                _mk_probe_result(name="provider-b"),
                _mk_probe_result(name="provider-c"),
            ],
        )
        monkeypatch.setattr(
            federation_nudge, "_already_adopted", lambda name: False,
        )
        monkeypatch.setattr(
            federation_nudge, "_has_declined", lambda name: False,
        )
        federation_nudge.maybe_render_federation_nudge()
        out = capsys.readouterr().out
        # First reachable provider mentioned by name
        assert "provider-a" in out
        # Avoid pelting the operator with three lines
        assert out.count("\n") <= 3


# ---------------------------------------------------------------------------
# Adoption check — reads llm-providers.toml
# ---------------------------------------------------------------------------


class TestAlreadyAdoptedHelper:
    def test_already_adopted_when_endpoint_in_llm_providers_toml(
        self, tmp_path,
    ):
        from axiom.extensions.builtins.chat import federation_nudge

        llm_providers = tmp_path / "llm-providers.toml"
        llm_providers.write_text("""
[[gateway.providers]]
name = "qwen-example"
endpoint = "https://example.local/v1"
default = true
""")
        with patch(
            "axiom.extensions.builtins.chat.federation_nudge._llm_providers_path",
            return_value=llm_providers,
        ):
            assert federation_nudge._already_adopted("qwen-example") is True

    def test_not_adopted_when_not_in_providers_file(self, tmp_path):
        from axiom.extensions.builtins.chat import federation_nudge

        llm_providers = tmp_path / "llm-providers.toml"
        llm_providers.write_text("""
[[gateway.providers]]
name = "anthropic-claude"
endpoint = "https://api.anthropic.com"
""")
        with patch(
            "axiom.extensions.builtins.chat.federation_nudge._llm_providers_path",
            return_value=llm_providers,
        ):
            assert federation_nudge._already_adopted("qwen-example") is False

    def test_not_adopted_when_providers_file_missing(self, tmp_path):
        from axiom.extensions.builtins.chat import federation_nudge

        missing = tmp_path / "does-not-exist.toml"
        with patch(
            "axiom.extensions.builtins.chat.federation_nudge._llm_providers_path",
            return_value=missing,
        ):
            assert federation_nudge._already_adopted("qwen-example") is False


# ---------------------------------------------------------------------------
# RAG plane (2026-09-02): the nudge also offers the site's shared RAG corpus
# + gold verbs when a reachable endpoint advertises a sibling RAG resource
# that rag.database_url is not yet pointed at.
# ---------------------------------------------------------------------------


def _mk_probe_with_rag(*, name="qwen-site", rag_endpoint="https://node.example:8767",
                       rag_corpus="reactor-corpus", reachable=True):
    from axiom.setup.federation_probe import ProbeResult
    conn = MagicMock()
    conn.name = name
    conn.endpoint = "https://node.example:8766/v1"
    conn.display_name = name
    return ProbeResult(connection=conn, reachable=reachable, latency_ms=90,
                       rag_corpus=rag_corpus, rag_endpoint=rag_endpoint)


class TestRagPlaneNudge:
    def _wire(self, monkeypatch, fed, *, candidates, adopted_llm, rag_adopted):
        monkeypatch.setattr(fed, "_is_tty", lambda: True)
        monkeypatch.setattr(fed, "_discover", lambda: candidates)
        monkeypatch.setattr(fed, "_already_adopted", lambda name: adopted_llm)
        monkeypatch.setattr(fed, "_has_declined", lambda name: False)
        monkeypatch.setattr(fed, "_rag_adopted", lambda ep: rag_adopted)

    def test_unadopted_llm_and_rag_prints_both_lines(self, capsys, monkeypatch):
        from axiom.extensions.builtins.chat import federation_nudge as fed
        self._wire(monkeypatch, fed, candidates=[_mk_probe_with_rag()],
                   adopted_llm=False, rag_adopted=False)
        fed.maybe_render_federation_nudge()
        out = capsys.readouterr().out
        assert "federation discover" in out            # LLM line
        assert "rag.database_url" in out               # RAG line
        assert "https://node.example:8767" in out      # the exact endpoint
        assert "gold verbs" in out

    def test_adopted_llm_but_unadopted_rag_still_nudges_rag(self, capsys, monkeypatch):
        from axiom.extensions.builtins.chat import federation_nudge as fed
        # LLM already adopted → usable is empty; RAG must still nudge.
        self._wire(monkeypatch, fed, candidates=[_mk_probe_with_rag()],
                   adopted_llm=True, rag_adopted=False)
        fed.maybe_render_federation_nudge()
        out = capsys.readouterr().out
        assert "rag.database_url" in out
        assert "federation discover" not in out        # no LLM line — already adopted

    def test_rag_already_adopted_prints_no_rag_line(self, capsys, monkeypatch):
        from axiom.extensions.builtins.chat import federation_nudge as fed
        self._wire(monkeypatch, fed, candidates=[_mk_probe_with_rag()],
                   adopted_llm=False, rag_adopted=True)
        fed.maybe_render_federation_nudge()
        out = capsys.readouterr().out
        assert "federation discover" in out            # LLM line still fires
        assert "rag.database_url" not in out            # RAG already pointed there

    def test_no_rag_sibling_prints_only_llm_line(self, capsys, monkeypatch):
        from axiom.extensions.builtins.chat import federation_nudge as fed
        no_rag = _mk_probe_with_rag(rag_endpoint=None, rag_corpus=None)
        self._wire(monkeypatch, fed, candidates=[no_rag],
                   adopted_llm=False, rag_adopted=False)
        fed.maybe_render_federation_nudge()
        out = capsys.readouterr().out
        assert "federation discover" in out
        assert "rag.database_url" not in out


class TestRagAdoptedHelper:
    def test_hostport_match_ignores_scheme_and_path(self, monkeypatch):
        from axiom.extensions.builtins.chat import federation_nudge as fed
        from axiom.extensions.builtins.settings import store as store_mod

        class _S:
            def get(self, k, d=None): return "https://node.example:8767/v1"
        monkeypatch.setattr(store_mod, "SettingsStore", _S)
        assert fed._rag_adopted("http://node.example:8767") is True
        assert fed._rag_adopted("https://other.host:8767") is False

    def test_unset_url_is_not_adopted(self, monkeypatch):
        from axiom.extensions.builtins.chat import federation_nudge as fed
        from axiom.extensions.builtins.settings import store as store_mod

        class _S:
            def get(self, k, d=None): return ""
        monkeypatch.setattr(store_mod, "SettingsStore", _S)
        assert fed._rag_adopted("https://node.example:8767") is False
