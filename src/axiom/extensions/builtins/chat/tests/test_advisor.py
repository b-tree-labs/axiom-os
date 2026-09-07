# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the post-command advisor hook.

The advisor OFFERS one dim tip after a turn; it never executes anything.
Its safety contract mirrors ``federation_nudge``: TTY-only, hard budget,
every exception swallowed, decline memo honoured, at most one tip.
"""

from __future__ import annotations

import dataclasses
import time
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.chat import advisor
from axiom.extensions.builtins.chat.advisor import (
    Advice,
    AdviceContext,
    maybe_advise,
    register_advisor,
)

# ---------------------------------------------------------------------------
# Rig
# ---------------------------------------------------------------------------


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Isolated advisor: tmp state dir, tmp project settings, empty registry,
    no entry points, TTY forced on."""
    from axiom.extensions.builtins.settings import store as store_mod

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(store_mod, "_PROJECT_SETTINGS_PATH", tmp_path / "project-settings.toml")
    monkeypatch.setattr(advisor, "_is_tty", lambda: True)
    monkeypatch.setattr(advisor, "_entry_points", lambda group: [])
    # These tests assert on contributor sequencing, not on the real-time
    # budget. A loaded CI runner can stall a bounded contributor long enough
    # to starve the default 400 ms and fail a sequencing assertion, so the
    # rig runs with a budget no scheduler hiccup can exhaust; the one test
    # about the budget passes its own explicitly.
    monkeypatch.setattr(advisor, "DEFAULT_BUDGET_MS", 10_000)
    advisor._reset_for_tests()
    yield tmp_path
    advisor._reset_for_tests()


def _ctx(turn_index: int = 1, **kw) -> AdviceContext:
    base = dict(
        command="/status",
        outcome="ok",
        elapsed_ms=12,
        session_id="s1",
        turn_index=turn_index,
        tool_names=(),
    )
    base.update(kw)
    return AdviceContext(**base)


class _Sink:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, text: str) -> None:
        self.lines.append(text)


def _tip(text: str, key: str, source: str = "t"):
    return lambda ctx: Advice(text=text, source=source, key=key)


# ---------------------------------------------------------------------------
# Gates: TTY and the enabled setting
# ---------------------------------------------------------------------------


def test_non_tty_calls_no_contributor(rig, monkeypatch):
    calls = []
    register_advisor("a", lambda ctx: calls.append(ctx) or None)
    monkeypatch.setattr(advisor, "_is_tty", lambda: False)
    sink = _Sink()
    assert maybe_advise(_ctx(), render=sink) is None
    assert calls == []
    assert sink.lines == []


def test_tty_override_for_tests_bypasses_isatty(rig, monkeypatch):
    calls = []
    register_advisor("a", lambda ctx: calls.append(ctx) or None)
    monkeypatch.setattr(advisor, "_is_tty", lambda: False)
    maybe_advise(_ctx(), render=_Sink(), tty=True)
    assert len(calls) == 1


def test_disabled_setting_calls_no_contributor(rig):
    calls = []
    register_advisor("a", lambda ctx: calls.append(ctx) or None)
    advisor.set_enabled(False)
    assert advisor.is_enabled() is False
    sink = _Sink()
    assert maybe_advise(_ctx(), render=sink) is None
    assert calls == []
    assert sink.lines == []


# ---------------------------------------------------------------------------
# One tip, in order, honouring memo and cooldown
# ---------------------------------------------------------------------------


def test_contributors_run_in_order_and_only_one_tip_renders(rig):
    order = []

    def first(ctx):
        order.append("first")
        return Advice(text="first tip", source="first", key="k1")

    def second(ctx):
        order.append("second")
        return Advice(text="second tip", source="second", key="k2")

    register_advisor("first", first)
    register_advisor("second", second)
    sink = _Sink()
    got = maybe_advise(_ctx(), render=sink)
    assert got is not None and got.key == "k1"
    assert sink.lines == ["first tip"]
    # The winner short-circuits: the second contributor is never consulted.
    assert order == ["first"]


def test_none_from_first_falls_through_to_second(rig):
    register_advisor("first", lambda ctx: None)
    register_advisor("second", _tip("second tip", "k2"))
    sink = _Sink()
    got = maybe_advise(_ctx(), render=sink)
    assert got is not None and got.source == "t" and got.key == "k2"
    assert sink.lines == ["second tip"]


def test_declined_key_is_skipped(rig):
    register_advisor("a", _tip("declined tip", "k-declined"))
    register_advisor("b", _tip("other tip", "k-other"))
    advisor.record_decline("k-declined")
    assert advisor.has_declined("k-declined") is True
    sink = _Sink()
    got = maybe_advise(_ctx(), render=sink)
    assert got is not None and got.key == "k-other"
    assert sink.lines == ["other tip"]


def test_decline_memo_persists_on_disk(rig):
    advisor.record_decline("k1")
    path = advisor._decline_path()
    assert path.exists()
    assert "k1" in path.read_text(encoding="utf-8")
    assert path.parent.name == "chat"


def test_cooldown_suppresses_repeat_for_n_turns(rig):
    register_advisor("a", _tip("same tip", "k-same"))
    sink = _Sink()
    assert maybe_advise(_ctx(turn_index=1), render=sink, cooldown_turns=3) is not None
    assert maybe_advise(_ctx(turn_index=2), render=sink, cooldown_turns=3) is None
    assert maybe_advise(_ctx(turn_index=3), render=sink, cooldown_turns=3) is None
    assert maybe_advise(_ctx(turn_index=4), render=sink, cooldown_turns=3) is not None
    assert sink.lines == ["same tip", "same tip"]


# ---------------------------------------------------------------------------
# Robustness: raising and slow contributors
# ---------------------------------------------------------------------------


def test_raising_contributor_is_swallowed_and_next_runs(rig):
    def boom(ctx):
        raise RuntimeError("contributor exploded")

    register_advisor("boom", boom)
    register_advisor("ok", _tip("still here", "k-ok"))
    sink = _Sink()
    got = maybe_advise(_ctx(), render=sink)
    assert got is not None and got.key == "k-ok"
    assert sink.lines == ["still here"]


def test_slow_contributor_is_abandoned_within_budget(rig):
    budget_ms = 200

    def slow(ctx):
        time.sleep(budget_ms * 5 / 1000)
        return Advice(text="too late", source="slow", key="k-slow")

    register_advisor("slow", slow)
    register_advisor("fast", _tip("fast tip", "k-fast"))
    sink = _Sink()
    t0 = time.monotonic()
    got = maybe_advise(_ctx(), render=sink, budget_ms=budget_ms)
    wall_ms = (time.monotonic() - t0) * 1000
    assert wall_ms < 2 * budget_ms
    # The slow one was abandoned; its late result is never rendered.
    assert "too late" not in sink.lines
    # The total budget is shared, so the fast one may or may not have had
    # time; whichever way, at most one line rendered.
    assert len(sink.lines) <= 1
    if got is not None:
        assert got.key == "k-fast"


def test_raising_render_is_swallowed(rig):
    register_advisor("a", _tip("tip", "k1"))

    def bad_render(text):
        raise OSError("stdout closed")

    # Must not raise; chat continues.
    maybe_advise(_ctx(), render=bad_render)


def test_contributor_returning_wrong_type_is_ignored(rig):
    register_advisor("bad", lambda ctx: "just a string")
    register_advisor("good", _tip("good", "k-good"))
    sink = _Sink()
    got = maybe_advise(_ctx(), render=sink)
    assert got is not None and got.key == "k-good"


# ---------------------------------------------------------------------------
# Advice is data, never code
# ---------------------------------------------------------------------------


def test_advice_with_callable_field_is_rejected():
    with pytest.raises(TypeError):
        Advice(text=lambda: "x", source="s", key="k")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Advice(text="x", source=print, key="k")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Advice(text="x", source="s", key=str.upper)  # type: ignore[arg-type]


def test_advice_is_one_line_and_capped_at_160_chars():
    long = "a" * 300
    a = Advice(text=long, source="s", key="k")
    assert len(a.text) <= 160
    b = Advice(text="line one\nline two\t tabbed", source="s", key="k")
    assert "\n" not in b.text and "\t" not in b.text
    assert b.text == "line one line two tabbed"


def test_advice_rejects_empty_text_or_key():
    with pytest.raises(ValueError):
        Advice(text="   ", source="s", key="k")
    with pytest.raises(ValueError):
        Advice(text="x", source="s", key="")


def test_advice_is_frozen():
    a = Advice(text="x", source="s", key="k")
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.text = "y"  # type: ignore[misc]


def test_context_rejects_unknown_outcome():
    with pytest.raises(ValueError):
        _ctx(outcome="maybe")


# ---------------------------------------------------------------------------
# Entry-point discovery
# ---------------------------------------------------------------------------


def test_entry_point_contributor_is_discovered(rig, monkeypatch):
    def plugin_advice(ctx):
        return Advice(text="from plugin", source="plugin", key="k-plugin")

    class _EP:
        name = "plugin"

        def load(self):
            return plugin_advice

    seen_groups = []

    def fake_entry_points(group):
        seen_groups.append(group)
        return [_EP()]

    monkeypatch.setattr(advisor, "_entry_points", fake_entry_points)
    advisor._reset_for_tests()
    sink = _Sink()
    got = maybe_advise(_ctx(), render=sink)
    assert seen_groups == [advisor.ADVISOR_GROUP]
    assert advisor.ADVISOR_GROUP == "axiom.chat.advisor"
    assert got is not None and got.key == "k-plugin"
    assert sink.lines == ["from plugin"]


def test_broken_entry_point_is_skipped(rig, monkeypatch):
    class _Broken:
        name = "broken"

        def load(self):
            raise ImportError("no such module")

    monkeypatch.setattr(advisor, "_entry_points", lambda group: [_Broken()])
    advisor._reset_for_tests()
    register_advisor("ok", _tip("ok", "k-ok"))
    got = maybe_advise(_ctx(), render=_Sink())
    assert got is not None and got.key == "k-ok"


def test_registry_runs_before_entry_points(rig, monkeypatch):
    class _EP:
        name = "plugin"

        def load(self):
            return _tip("plugin", "k-plugin")

    monkeypatch.setattr(advisor, "_entry_points", lambda group: [_EP()])
    advisor._reset_for_tests()
    register_advisor("builtin", _tip("builtin", "k-builtin"))
    got = maybe_advise(_ctx(), render=_Sink())
    assert got is not None and got.key == "k-builtin"


# ---------------------------------------------------------------------------
# The local-SLM contributor
# ---------------------------------------------------------------------------


@pytest.fixture
def no_cloud(monkeypatch):
    """Any attempt to build a Gateway (the cloud path) is a test failure."""
    from axiom.infra import gateway as gw

    def _boom(*a, **k):
        raise AssertionError("advisor must never touch the cloud gateway")

    monkeypatch.setattr(gw.Gateway, "__init__", _boom)


def test_slm_is_off_by_default_and_never_calls_model(rig, no_cloud, monkeypatch):
    from axiom.extensions.builtins.chat import slm_advisor

    called = []
    monkeypatch.setattr(slm_advisor, "_local_complete", lambda *a, **k: called.append(1) or "x")
    assert slm_advisor.slm_enabled() is False
    ctx = _ctx(outcome="error", tool_names=("read_file",))
    assert slm_advisor.local_slm_advice(ctx) is None
    assert called == []


def test_slm_on_with_fake_local_completion_truncates_to_120(rig, no_cloud, monkeypatch):
    from axiom.extensions.builtins.chat import slm_advisor

    slm_advisor.set_slm_enabled(True)
    assert slm_advisor.slm_enabled() is True
    seen = {}

    def fake_complete(prompt, *, system, timeout_s):
        seen["prompt"] = prompt
        seen["timeout_s"] = timeout_s
        return "Try running the command again with --verbose. " * 10

    monkeypatch.setattr(slm_advisor, "_local_complete", fake_complete)
    ctx = _ctx(command="/doctor", outcome="error", tool_names=("read_file", "grep"))
    got = slm_advisor.local_slm_advice(ctx)
    assert isinstance(got, Advice)
    assert len(got.text) <= 120
    assert got.source == "local_slm"
    assert got.key.startswith("slm:")
    assert seen["timeout_s"] <= 0.3
    assert "/doctor" in seen["prompt"] and "read_file" in seen["prompt"]


def test_slm_key_is_stable_hash_of_command_and_outcome(rig, no_cloud, monkeypatch):
    from axiom.extensions.builtins.chat import slm_advisor

    slm_advisor.set_slm_enabled(True)
    monkeypatch.setattr(slm_advisor, "_local_complete", lambda *a, **k: "next step")
    a = slm_advisor.local_slm_advice(_ctx(command="/x", outcome="error"))
    b = slm_advisor.local_slm_advice(_ctx(command="/x", outcome="error", turn_index=9))
    c = slm_advisor.local_slm_advice(_ctx(command="/y", outcome="error"))
    assert a.key == b.key
    assert a.key != c.key


def test_slm_skips_plain_ok_turns_without_tools(rig, no_cloud, monkeypatch):
    from axiom.extensions.builtins.chat import slm_advisor

    slm_advisor.set_slm_enabled(True)
    called = []
    monkeypatch.setattr(slm_advisor, "_local_complete", lambda *a, **k: called.append(1) or "x")
    assert slm_advisor.local_slm_advice(_ctx(outcome="ok", tool_names=())) is None
    assert called == []


def test_slm_unreachable_ollama_returns_none(rig, no_cloud, monkeypatch):
    import urllib.error
    import urllib.request

    from axiom.extensions.builtins.chat import slm_advisor

    slm_advisor.set_slm_enabled(True)
    urls = []

    def refuse(req, timeout=None):
        urls.append(req.full_url)
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    ctx = _ctx(outcome="error", tool_names=("x",))
    assert slm_advisor.local_slm_advice(ctx) is None
    # It reached for the local Ollama path and nothing else.
    assert len(urls) == 1
    assert urls[0].endswith("/api/generate")
    assert "localhost" in urls[0] or "127.0.0.1" in urls[0]


def test_slm_never_autostarts_ollama(rig, no_cloud, monkeypatch):
    import urllib.error
    import urllib.request

    from axiom.extensions.builtins.chat import slm_advisor
    from axiom.infra import connections

    slm_advisor.set_slm_enabled(True)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("down")),
    )

    def _no(*a, **k):
        raise AssertionError("advisor must not auto-start a service")

    monkeypatch.setattr(connections, "ensure_available", _no, raising=False)
    assert slm_advisor.local_slm_advice(_ctx(outcome="error")) is None


# ---------------------------------------------------------------------------
# /advisor slash command
# ---------------------------------------------------------------------------


def test_advisor_off_persists_and_maybe_advise_returns_none(rig):
    from axiom.extensions.builtins.chat.cli import _handle_slash_command
    from axiom.extensions.builtins.settings.store import SettingsStore

    calls = []
    register_advisor("a", lambda ctx: calls.append(1) or Advice(text="t", source="a", key="k"))
    agent = SimpleNamespace(session=SimpleNamespace(session_id="s", messages=[]))
    store = SimpleNamespace()

    out = _handle_slash_command("/advisor off", agent, store)
    assert "off" in out.lower()
    assert SettingsStore().get("chat.advisor.enabled") is False
    assert maybe_advise(_ctx(), render=_Sink()) is None
    assert calls == []

    out = _handle_slash_command("/advisor on", agent, store)
    assert "on" in out.lower()
    assert SettingsStore().get("chat.advisor.enabled") is True
    assert maybe_advise(_ctx(), render=_Sink()) is not None


def test_advisor_status_reports_state(rig):
    from axiom.extensions.builtins.chat.commands import cmd_advisor

    out = cmd_advisor([])
    assert "on" in out.lower()
    assert "slm" in out.lower()
    advisor.set_enabled(False)
    assert "off" in cmd_advisor(["status"]).lower()


def test_advisor_mute_declines_last_shown_tip(rig):
    from axiom.extensions.builtins.chat.commands import cmd_advisor

    register_advisor("a", _tip("tip", "k-mute"))
    assert maybe_advise(_ctx(turn_index=1), render=_Sink()) is not None
    out = cmd_advisor(["mute"])
    assert "k-mute" in out or "won't" in out.lower() or "muted" in out.lower()
    assert advisor.has_declined("k-mute") is True
    assert maybe_advise(_ctx(turn_index=100), render=_Sink()) is None


def test_advisor_is_a_documented_slash_command():
    from axiom.extensions.builtins.chat.commands import cmd_help, get_slash_commands

    assert "/advisor" in get_slash_commands()
    assert "/advisor" in cmd_help()


# ---------------------------------------------------------------------------
# advise_turn: the shared front-end helper builds the context from the agent
# ---------------------------------------------------------------------------


def test_advise_turn_builds_context_from_agent(rig):
    seen: list[AdviceContext] = []

    def spy(ctx):
        seen.append(ctx)
        return None

    register_advisor("spy", spy)
    agent = SimpleNamespace(
        session=SimpleNamespace(session_id="sess-42", messages=[]),
        last_turn_tools=["read_file", "grep"],
    )
    advisor.advise_turn(
        agent,
        command="x" * 500,
        outcome="ok",
        elapsed_ms=1234.9,
        turn_index=3,
        render=_Sink(),
    )
    assert len(seen) == 1
    ctx = seen[0]
    assert ctx.session_id == "sess-42"
    assert ctx.tool_names == ("read_file", "grep")
    assert ctx.turn_index == 3
    assert ctx.elapsed_ms == 1234
    assert len(ctx.command) <= advisor.MAX_COMMAND_CHARS


def test_advise_turn_never_raises_on_a_broken_agent(rig):
    assert (
        advisor.advise_turn(
            None, command="x", outcome="ok", elapsed_ms=0, turn_index=1, render=_Sink()
        )
        is None
    )


def test_agent_records_tool_names_per_turn():
    """The agent exposes which tools ran in the last turn (the advisor's input)."""
    from axiom.extensions.builtins.chat.agent import ChatAgent

    assert "last_turn_tools" in ChatAgent.__init__.__code__.co_names or hasattr(
        ChatAgent, "last_turn_tools"
    )


# ---------------------------------------------------------------------------
# Federation nudge as the first built-in contributor
# ---------------------------------------------------------------------------


def _mk_probe_result(*, name="qwen-example", rag_endpoint=None, rag_corpus=None):
    from unittest.mock import MagicMock

    from axiom.setup.federation_probe import ProbeResult

    conn = MagicMock()
    conn.name = name
    conn.endpoint = "https://example.local/v1"
    conn.display_name = name
    return ProbeResult(
        connection=conn,
        reachable=True,
        latency_ms=100,
        rag_corpus=rag_corpus,
        rag_endpoint=rag_endpoint,
    )


def test_federation_contributor_offers_adopt_tip(rig, monkeypatch):
    from axiom.extensions.builtins.chat import federation_nudge as fed

    fed._reset_advisor_cache_for_tests()
    monkeypatch.setattr(fed, "_discover", lambda: [_mk_probe_result()])
    monkeypatch.setattr(fed, "_already_adopted", lambda name: False)
    monkeypatch.setattr(fed, "_has_declined", lambda name: False)
    got = fed.federation_advice(_ctx())
    assert isinstance(got, Advice)
    assert "qwen-example" in got.text
    assert "federation discover" in got.text
    assert got.source == "federation"
    assert got.key == "federation:qwen-example"


def test_federation_contributor_is_silent_when_nothing_to_adopt(rig, monkeypatch):
    from axiom.extensions.builtins.chat import federation_nudge as fed

    fed._reset_advisor_cache_for_tests()
    monkeypatch.setattr(fed, "_discover", lambda: [])
    assert fed.federation_advice(_ctx()) is None


def test_federation_contributor_throttles_the_probe(rig, monkeypatch):
    from axiom.extensions.builtins.chat import federation_nudge as fed

    fed._reset_advisor_cache_for_tests()
    probes = []
    monkeypatch.setattr(fed, "_discover", lambda: probes.append(1) or [])
    fed.federation_advice(_ctx(turn_index=1))
    fed.federation_advice(_ctx(turn_index=2))
    assert len(probes) == 1


def test_federation_contributor_swallows_probe_errors(rig, monkeypatch):
    from axiom.extensions.builtins.chat import federation_nudge as fed

    fed._reset_advisor_cache_for_tests()
    monkeypatch.setattr(fed, "_discover", lambda: (_ for _ in ()).throw(RuntimeError("net down")))
    assert fed.federation_advice(_ctx()) is None


def test_builtins_are_registered_in_order(rig):
    advisor._reset_for_tests(builtins=True)
    names = [n for n, _ in advisor._contributors()]
    assert names[:2] == ["federation", "local_slm"]
