# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Routing build plan, Phase 0 acceptance, P0.1 streaming parity (RS-E3).

docs/working/routing-build-plan-2026-11-12.md, section 3. The /app chat streams, so the streamed
path is the product's chat. It must pass the same four checks as ``complete_with_tools``:

- P0.1-A1: a streamed turn writes the same routing audit record as a non-streamed one.
- P0.1-A2: prompt hardening applies on the streamed path for a controlled label.
- P0.1-A3: the response scan applies to streamed text.
- P0.1-A4: a streamed controlled request with no attributed principal is refused.

Each test runs the same request down both paths and compares what they wrote. The provider is a
recorder; nothing reaches the network.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from axiom.llm import gateway as gw

_REPLY = "The HEU inventory is reported quarterly."  # "HEU" is on the classified-terms list
_CLEAN = "The loop ran nominally."
_SYSTEM = "You are the facility assistant."
_MESSAGES = [{"role": "user", "content": "what is the inventory?"}]


def _provider(
    tier: str, name: str = "site-model", endpoint: str = "https://llm.example.internal/v1"
):
    return gw.LLMProvider(
        name=name,
        endpoint=endpoint,
        model="m",
        uid=f"uid-{name}",
        api_key_env="P01_FAKE_KEY",
        priority=1,
        use_for=["chat", "fallback"],
        routing_tier=tier,
    )


class _Recorder:
    """What the provider was sent, and the reply it gives on either path."""

    def __init__(self, reply: str):
        self.reply = reply
        self.systems: list[tuple[str, str]] = []  # (path, system prompt the provider received)
        self.streamed = 0

    def complete(self, provider, messages, system, tools, max_tokens):
        self.systems.append(("complete", system))
        return gw.CompletionResponse(
            text=self.reply, provider=provider.name, model=provider.model, success=True
        )

    def stream(self, provider, messages, system, tools, max_tokens):
        self.systems.append(("stream", system))
        self.streamed += 1
        words = self.reply.split(" ")
        for i, word in enumerate(words):
            yield gw.StreamChunk(type="text", text=word if i == len(words) - 1 else word + " ")
        yield gw.StreamChunk(type="usage", input_tokens=3, output_tokens=5)
        yield gw.StreamChunk(type="done")


@pytest.fixture
def logs(monkeypatch):
    """Capture the routing audit and the security log. Returns ``(routing, scans)``."""
    routing: list[dict] = []
    scans: list[dict] = []

    class _Audit:
        def write_routing(self, **kw):
            routing.append(kw)

        def write_vpn(self, **kw):
            pass

    class _Security:
        def response_scan_hit(self, **kw):
            scans.append(kw)

    from axiom.infra import audit_log, security_log

    monkeypatch.setattr(audit_log.AuditLog, "get", classmethod(lambda cls: _Audit()))
    monkeypatch.setattr(security_log.SecurityLog, "get", classmethod(lambda cls: _Security()))
    monkeypatch.setenv("P01_FAKE_KEY", "sk-test-not-real")
    monkeypatch.setenv("AXIOM_ACTOR", "@casey:site")
    return routing, scans


def _gateway(provider, recorder: _Recorder):
    with patch.object(gw.Gateway, "_load_config", lambda self: None):
        g = gw.Gateway()
    g.providers = [provider]
    g._trace_call = lambda *a, **k: None
    g._trace_generation = lambda *a, **k: None
    g._call_provider_with_tools = recorder.complete
    g._stream_provider = recorder.stream
    return g


def _streamed_text(chunks) -> str:
    return "".join(c.text for c in chunks if c.type == "text")


def _both_paths(tier: str, reply: str, provider=None):
    """Run the same request non-streamed then streamed. Returns (recorder, complete_text, streamed_text)."""
    rec = _Recorder(reply)
    g = _gateway(provider or _provider(tier), rec)
    done = g.complete_with_tools(list(_MESSAGES), system=_SYSTEM, routing_tier=tier)
    streamed = _streamed_text(
        g.stream_with_tools(list(_MESSAGES), system=_SYSTEM, routing_tier=tier)
    )
    return rec, done.text, streamed


# ---------------------------------------------------------------------------
# P0.1-A1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", ["public", "export_controlled"])
def test_p0_1_a1_a_streamed_turn_writes_the_same_routing_audit_record(logs, tier):
    """P0.1-A1: one record per turn on each path, and the two records are equal field for field."""
    routing, _scans = logs

    _both_paths(tier, _CLEAN)

    assert len(routing) == 2, f"expected one routing record per path, got {len(routing)}: {routing}"
    non_streamed, streamed = routing
    assert streamed == non_streamed
    assert streamed["principal"] == "@casey:site"
    assert streamed["blocked"] is False


def test_p0_1_a1_a_streamed_reply_with_a_scan_hit_is_recorded_the_same_way(logs):
    """P0.1-A1: the response hash is of the model's reply on both paths, so a scan hit does not split them."""
    routing, _scans = logs

    _both_paths("public", _REPLY)

    assert len(routing) == 2
    assert routing[0] == routing[1]


def test_p0_1_a1_a_streamed_headless_backend_turn_is_recorded(logs):
    """P0.1-A1: the headless Claude Code backend answers in one chunk; it is still audited."""
    routing, _scans = logs
    rec = _Recorder(_CLEAN)
    g = _gateway(_provider("public", name="cc", endpoint="claude-code://local"), rec)
    g._call_claude_code_with_tools = lambda provider, messages, system: rec.complete(
        provider, messages, system, None, 0
    )

    _streamed_text(g.stream_with_tools(list(_MESSAGES), system=_SYSTEM, routing_tier="public"))

    assert len(routing) == 1 and routing[0]["provider_name"] == "cc"


# ---------------------------------------------------------------------------
# P0.1-A2
# ---------------------------------------------------------------------------


def test_p0_1_a2_prompt_hardening_applies_on_the_streamed_path_for_a_controlled_label(logs):
    """P0.1-A2: the streamed provider receives the hardened system prompt, as the non-streamed one does."""
    rec, _done, _streamed = _both_paths("export_controlled", _CLEAN)

    hardened = gw._harden_system_prompt(_SYSTEM)
    assert rec.systems == [("complete", hardened), ("stream", hardened)]


def test_p0_1_a2_a_public_streamed_turn_is_not_hardened(logs):
    """P0.1-A2: hardening is for the controlled label only, on both paths."""
    rec, _done, _streamed = _both_paths("public", _CLEAN)

    assert rec.systems == [("complete", _SYSTEM), ("stream", _SYSTEM)]


# ---------------------------------------------------------------------------
# P0.1-A3
# ---------------------------------------------------------------------------


def test_p0_1_a3_the_response_scan_applies_to_streamed_text(logs):
    """P0.1-A3: a classified term in streamed text is logged exactly as on the non-streamed path, and the
    person is warned in the reply. The streamed warning follows the text, since the text is already shown."""
    _routing, scans = logs

    _rec, done, streamed = _both_paths("public", _REPLY)

    assert len(scans) == 2, f"expected one scan hit per path, got {scans}"
    assert scans[0] == scans[1]
    assert done.startswith("[SECURITY WARNING")
    assert streamed.startswith(_REPLY.split(" ")[0]) and "[SECURITY WARNING" in streamed
    assert streamed.rstrip().endswith("flagged for review.]"), (
        "the warning follows the streamed text"
    )


def test_p0_1_a3_a_clean_streamed_reply_is_unchanged(logs):
    """P0.1-A3: no hit, no log entry, no warning."""
    _routing, scans = logs

    _rec, _done, streamed = _both_paths("public", _CLEAN)

    assert scans == []
    assert streamed.strip() == _CLEAN


def test_p0_1_a3_the_scan_adds_no_delay_before_the_first_token(logs, monkeypatch):
    """P0.1-A3: the scan runs on the assembled reply after the last text chunk, never before the first one.

    Ordering is the deterministic form of "no noticeable delay": the first chunk reaches the caller before
    the scanner has been called at all. (A timing note in the spec gives the measured pre-dispatch cost.)
    """
    _routing, scans = logs
    calls: list[str] = []
    from axiom.llm.router import QueryRouter

    real = QueryRouter._keyword_check

    def _spy(self, text):
        calls.append(text)
        return real(self, text)

    monkeypatch.setattr(QueryRouter, "_keyword_check", _spy)
    rec = _Recorder(_REPLY)
    g = _gateway(_provider("public"), rec)

    stream = g.stream_with_tools(list(_MESSAGES), system=_SYSTEM, routing_tier="public")
    first = next(stream)
    assert first.type == "text" and calls == [], (
        "the scanner ran before the first token was delivered"
    )

    rest = list(stream)
    assert calls == [
        "".join(c.text for c in [first, *rest] if c.type == "text" and "[SECURITY" not in c.text)
    ]
    assert rest[-1].type == "done"


# ---------------------------------------------------------------------------
# P0.1-A4
# ---------------------------------------------------------------------------


def test_p0_1_a4_a_streamed_controlled_request_with_no_attributed_principal_is_refused(
    logs, monkeypatch
):
    """P0.1-A4: nothing is sent, the refusal says why, and it is audited as on the non-streamed path."""
    routing, _scans = logs
    monkeypatch.delenv("AXIOM_ACTOR", raising=False)
    monkeypatch.setattr(gw, "_resolve_actor_handle", lambda: None)
    rec = _Recorder(_CLEAN)
    g = _gateway(_provider("export_controlled"), rec)

    done = g.complete_with_tools(list(_MESSAGES), system=_SYSTEM, routing_tier="export_controlled")
    chunks = list(
        g.stream_with_tools(list(_MESSAGES), system=_SYSTEM, routing_tier="export_controlled")
    )

    assert rec.systems == [], "an unattributed controlled request reached the provider"
    assert done.error == "EC_UNATTRIBUTED"
    assert _streamed_text(chunks) == done.text
    assert chunks[-1].type == "done"
    assert len(routing) == 2 and routing[0] == routing[1]
    assert routing[1]["block_reason"] == "EC_UNATTRIBUTED" and routing[1]["blocked"] is True


def test_p0_1_a4_an_explicit_principal_admits_a_streamed_controlled_request(logs, monkeypatch):
    """P0.1-A4: ``stream_with_tools`` takes ``principal=`` as ``complete_with_tools`` does."""
    routing, _scans = logs
    monkeypatch.delenv("AXIOM_ACTOR", raising=False)
    monkeypatch.setattr(gw, "_resolve_actor_handle", lambda: None)
    rec = _Recorder(_CLEAN)
    g = _gateway(_provider("export_controlled"), rec)

    out = _streamed_text(
        g.stream_with_tools(
            list(_MESSAGES), system=_SYSTEM, routing_tier="export_controlled", principal="@ben:site"
        )
    )

    assert out.strip() == _CLEAN
    assert routing[-1]["principal"] == "@ben:site" and routing[-1]["blocked"] is False
