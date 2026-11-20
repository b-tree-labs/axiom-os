# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Slash-command completion: one engine behind the dropdown, the gray ghost text, and both surfaces.

The prompt_toolkit objects here are real (Document, Buffer, Completer, AutoSuggest); nothing is mocked.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("prompt_toolkit")

from prompt_toolkit.buffer import Buffer  # noqa: E402
from prompt_toolkit.document import Document  # noqa: E402

from axiom.extensions.builtins.chat import completion  # noqa: E402
from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded  # noqa: E402
from axiom.extensions.builtins.chat.command_registry import REGISTRY, Command  # noqa: E402
from axiom.extensions.builtins.chat.completion import (  # noqa: E402
    SlashAutoSuggest,
    SlashCompleter,
    accept_suggestion,
    best_remainder,
    record_use,
    suggest,
)
from axiom.infra.orchestrator.session import Session  # noqa: E402
from tests.test_gateway import _gateway_with_providers, _make_provider  # noqa: E402


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    completion._usage_cache = None
    ensure_loaded()
    yield
    completion._usage_cache = None


def _values(line, **kw):
    return [s.value for s in suggest(line, **kw)]


def _agent(monkeypatch):
    providers = [
        _make_provider(
            name="home", endpoint="http://localhost:8080/v1", api_key_env="", priority=1
        ),
        _make_provider(
            name="cloudy",
            endpoint="https://api.anthropic.com/v1",
            routing_tier="public",
            priority=2,
        ),
    ]
    return SimpleNamespace(
        gateway=_gateway_with_providers(providers, monkeypatch), session=Session()
    )


# ---- the pure engine ----------------------------------------------------------------------------


def test_exact_prefix_ranks_before_fuzzy_matches():
    v = _values("/mo")
    assert v[0] == "/model"
    assert v.index("/model") < v.index("/permissions")  # permissions matches only as a subsequence


def test_fuzzy_finds_a_command_typed_with_letters_missing():
    assert "/model" in _values("/mdl")


def test_matching_ignores_case():
    assert _values("/MOD")[0] == "/model"


def test_most_used_command_wins_a_tie_between_prefix_matches():
    before = _values("/s")
    other = next(v for v in before if v != before[0])
    for _ in range(3):
        record_use(other)
    assert _values("/s")[0] == other


def test_usage_counter_is_bounded_and_never_stores_arguments(tmp_path):
    for i in range(completion.MAX_USAGE_ENTRIES + 40):
        record_use(f"/cmd{i} sub secret-argument-{i}")
    raw = (tmp_path / "state" / completion._USAGE_FILE).read_text()
    data = json.loads(raw)
    assert len(data) <= completion.MAX_USAGE_ENTRIES
    assert "secret-argument" not in raw


def test_a_corrupt_usage_file_is_an_empty_counter(tmp_path):
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / completion._USAGE_FILE).write_text("{not json")
    assert "/model" in _values("/mo")


def test_prose_and_multiline_get_nothing():
    assert suggest("hello there") == []
    assert suggest("/model\nsecond line") == []
    assert suggest("") == []


def test_unknown_command_gets_no_argument_suggestions():
    assert suggest("/nonesuch ") == []


def test_admin_only_command_is_hidden_from_a_non_admin_and_offered_to_an_admin(monkeypatch):
    monkeypatch.setenv("AXI_ADMINS", "someone-else")
    from axiom.extensions.builtins.chat import roles

    monkeypatch.setattr(roles, "_configured_admins", lambda: {"boss"})
    plain = SimpleNamespace(session=SimpleNamespace(principal_id="guest"))
    boss = SimpleNamespace(session=SimpleNamespace(principal_id="boss"))
    assert "/trace" not in _values("/tr", agent=plain)
    assert suggest("/trace ", agent=plain) == []  # its subcommands do not leak either
    assert "/trace" in _values("/tr", agent=boss)
    assert {"on", "off", "status"} <= set(_values("/trace ", agent=boss))


def test_a_command_the_surface_cannot_run_is_not_suggested():
    cmd = Command(
        "/tuionly", "only in the tui", "System", lambda c, a: "x", surfaces=frozenset({"tui"})
    )
    REGISTRY.register(cmd)
    try:
        assert "/tuionly" in _values("/tuio", surface="tui")
        assert "/tuionly" not in _values("/tuio", surface="repl")
    finally:
        REGISTRY._by_name.pop("/tuionly")
        REGISTRY._order.remove(cmd)


def test_subcommands_are_offered_after_the_command():
    assert set(_values("/sessions ")) == {"rename", "archive"}
    assert _values("/sessions ren") == ["rename"]


def test_model_arguments_are_providers_labelled_with_placement(monkeypatch):
    got = {s.value: s.description for s in suggest("/model ", agent=_agent(monkeypatch))}
    assert set(got) == {"home", "cloudy"}
    assert "LOCAL" in got["home"] and "CLOUD" in got["cloudy"]
    assert _values("/model cl", agent=_agent(monkeypatch)) == ["cloudy"]


def test_permissions_walk_set_then_tool_then_mode(monkeypatch):
    a = _agent(monkeypatch)
    assert set(_values("/permissions ", agent=a)) == {"set", "reset", "held"}
    tools = _values("/permissions set ", agent=a)
    assert "read_file" in tools
    assert set(_values("/permissions set read_file ", agent=a)) == {"allow", "ask", "deny"}
    assert _values("/permissions set read_file de", agent=a) == ["deny"]
    assert "read_file" in _values("/permissions reset ", agent=a)


def test_resume_offers_recent_sessions_by_id_with_the_title(tmp_path, monkeypatch):
    from axiom.infra.orchestrator import session as sess

    (tmp_path / "sessions").mkdir()
    store = sess.SessionStore(tmp_path / "sessions")
    s = store.create()
    s.title = "Pump trip review"
    s.add_message("user", "hello")
    store.save(s)
    monkeypatch.setattr(
        sess.SessionStore,
        "__init__",
        lambda self, sessions_dir=None: setattr(self, "_dir", tmp_path / "sessions"),
    )
    got = {x.value: x.description for x in suggest("/resume ")}
    assert got[s.session_id] == "Pump trip review"
    assert s.session_id in {x.value for x in suggest("/sessions archive ")}


def test_where_rag_and_trace_flags():
    assert _values("/where ") == ["--json"]
    assert set(_values("/rag o")) == {"on", "off"}


def test_a_failing_completer_does_not_break_typing(monkeypatch):
    def boom(agent, args):
        raise RuntimeError("nope")

    cmd = Command("/boomy", "x", "System", lambda c, a: "", complete=boom)
    REGISTRY.register(cmd)
    try:
        assert suggest("/boomy ") == []
    finally:
        REGISTRY._by_name.pop("/boomy")
        REGISTRY._order.remove(cmd)


def test_an_existing_completer_is_not_overridden():
    from axiom.extensions.builtins.chat.completers import wire_completers

    mine = lambda agent, args: [("mine", "")]  # noqa: E731
    REGISTRY.set_completer("/where", mine)
    try:
        wire_completers(REGISTRY)
        assert REGISTRY.get("/where").complete is mine
    finally:
        from axiom.extensions.builtins.chat.completers import complete_where

        REGISTRY.set_completer("/where", complete_where)


# ---- ghost text ---------------------------------------------------------------------------------


def _ghost(text):
    buf = Buffer()
    doc = Document(text, cursor_position=len(text))
    sug = SlashAutoSuggest().get_suggestion(buf, doc)
    return sug.text if sug else None


def test_ghost_text_is_the_rest_of_the_best_completion():
    assert _ghost("/mod") == "el"
    assert _ghost("/sessions ren") == "ame"


def test_ghost_text_follows_usage():
    base = suggest("/s")
    runner_up = next(s.value for s in base if s.value != base[0].value)
    for _ in range(3):
        record_use(runner_up)
    assert _ghost("/s") == runner_up[2:]


def test_no_ghost_for_prose_or_a_mid_line_cursor_or_a_fuzzy_only_match():
    assert _ghost("model") is None
    assert _ghost("tell me about /mod") is None
    assert _ghost("") is None
    assert _ghost("/mdl") is None  # fuzzy matches are in the dropdown, never gray text
    doc = Document("/mod", cursor_position=2)
    assert SlashAutoSuggest().get_suggestion(Buffer(), doc) is None


def test_ghost_text_hides_commands_the_viewer_cannot_see(monkeypatch):
    from axiom.extensions.builtins.chat import roles

    monkeypatch.setattr(roles, "_configured_admins", lambda: {"boss"})
    plain = SimpleNamespace(session=SimpleNamespace(principal_id="guest"))
    assert best_remainder("/trac", plain, "repl") == ""
    boss = SimpleNamespace(session=SimpleNamespace(principal_id="boss"))
    assert best_remainder("/trac", boss, "repl") == "e"


# ---- dropdown -----------------------------------------------------------------------------------


def test_completer_yields_descriptions_as_meta_and_replaces_the_typed_word():
    doc = Document("/he", cursor_position=3)
    items = list(SlashCompleter().get_completions(doc, None))
    help_ = next(c for c in items if c.text == "/help")
    assert "command" in help_.display_meta_text.lower()
    assert help_.start_position == -3


def test_completer_is_silent_for_prose():
    assert list(SlashCompleter().get_completions(Document("hel", cursor_position=3), None)) == []


def test_completer_replaces_only_the_argument_word(monkeypatch):
    doc = Document("/model cl", cursor_position=9)
    c = next(iter(SlashCompleter(_agent(monkeypatch)).get_completions(doc, None)))
    assert (c.text, c.start_position) == ("cloudy", -2)


# ---- real prompt_toolkit round trips (a running Application, real key input) -----------------------


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def _app_with(buf, inp):
    from prompt_toolkit.application import Application
    from prompt_toolkit.layout import Layout, Window
    from prompt_toolkit.layout.controls import BufferControl
    from prompt_toolkit.output import DummyOutput

    return Application(layout=Layout(Window(BufferControl(buf))), input=inp, output=DummyOutput())


def test_a_buffer_computes_the_ghost_text_itself_and_accept_inserts_it():
    import asyncio

    async def go():
        buf = Buffer(auto_suggest=SlashAutoSuggest(), completer=SlashCompleter())
        from prompt_toolkit.input import create_pipe_input

        with create_pipe_input() as inp:
            app = _app_with(buf, inp)
            task = asyncio.create_task(app.run_async())
            await asyncio.sleep(0.1)
            buf.insert_text("/mod")
            await asyncio.sleep(0.2)
            seen = buf.suggestion.text if buf.suggestion else None
            accepted = accept_suggestion(buf)
            after = buf.text
            again = accept_suggestion(buf)
            app.exit()
            await task
        return seen, accepted, after, again

    seen, accepted, after, again = _run(go())
    assert seen == "el" and accepted and after == "/model" and not again


def test_a_buffer_gets_no_ghost_text_for_prose():
    import asyncio

    async def go():
        buf = Buffer(auto_suggest=SlashAutoSuggest())
        from prompt_toolkit.input import create_pipe_input

        with create_pipe_input() as inp:
            app = _app_with(buf, inp)
            task = asyncio.create_task(app.run_async())
            await asyncio.sleep(0.1)
            buf.insert_text("mod")
            await asyncio.sleep(0.2)
            seen = buf.suggestion
            app.exit()
            await task
        return seen

    assert _run(go()) is None


def test_repl_tab_accepts_the_gray_text_through_the_real_prompt_session():
    """The REPL's own key bindings, driven by key presses: type /mod, Tab, Enter -> /model."""
    import asyncio

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from axiom.extensions.builtins.chat.providers.ptk_input import PTKInputProvider

    async def go():
        with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
            prov = PTKInputProvider()
            prov.setup(slash_commands={"/model": "x"})
            task = asyncio.create_task(prov._session.prompt_async("you> "))
            await asyncio.sleep(0.2)
            inp.send_text("/mod")
            await asyncio.sleep(0.3)
            inp.send_text("\t")
            await asyncio.sleep(0.2)
            inp.send_text("\r")
            return await asyncio.wait_for(task, 5), prov.drain_queue()

    text, queued = _run(go())
    assert text == "/model" and queued == []


def test_the_repl_dropdown_and_the_ghost_text_come_from_the_same_engine():
    from axiom.extensions.builtins.chat.providers.ptk_input import ChatCompleter

    c = ChatCompleter(slash_commands={"/stale": "an old palette entry"})
    c.attach_engine(SlashCompleter())
    doc = Document("/mo", cursor_position=3)
    shown = [x.text for x in c.get_completions(doc, None)]
    assert shown[0] == "/model" and "/stale" not in shown
    assert _ghost("/mo") == shown[0][3:]


def test_without_prompt_toolkit_the_factories_return_none(monkeypatch):
    monkeypatch.setattr(completion, "_PTK", False)
    assert completion.make_autosuggest() is None and completion.make_completer() is None


# ---- the full-screen TUI uses the same engine and its own key bindings ---------------------------


def _tui():
    from axiom.extensions.builtins.chat.fullscreen import FullScreenChat

    agent = SimpleNamespace(
        session=SimpleNamespace(id="t", messages=[], title="t", principal_id="guest"),
        cancel=lambda: None,
        allowlisted_tools=lambda: [],
    )
    store = SimpleNamespace(list_sessions=lambda **kw: [], save=lambda *a, **k: None)
    tui = FullScreenChat(agent=agent, store=store, stream=True)
    tui._app = tui._build_app()
    return tui


def _binding(tui, key):
    live = [
        b
        for b in tui._app.key_bindings.bindings
        if [getattr(k, "value", k) for k in b.keys] == [key]
    ]
    assert live, key
    return live[-1]


def _press(tui, key):
    b = _binding(tui, key)
    b.handler(
        SimpleNamespace(
            current_buffer=tui._input_buffer, app=SimpleNamespace(invalidate=lambda: None)
        )
    )


def test_the_tui_buffer_is_wired_to_the_shared_engine():
    tui = _tui()
    buf = tui._input_buffer
    assert isinstance(buf.auto_suggest, SlashAutoSuggest)
    assert isinstance(buf.completer, SlashCompleter)


def test_tui_tab_and_right_arrow_accept_the_gray_text():
    import asyncio

    async def go():
        tui = _tui()
        buf = tui._input_buffer
        out = {}
        for key in ("c-i", "right"):
            buf.reset()
            buf.insert_text("/mod")
            buf.suggestion = buf.auto_suggest.get_suggestion(buf, buf.document)
            ghost = buf.suggestion.text
            _press(tui, key)
            out[key] = (ghost, buf.text)
        await asyncio.sleep(0.05)
        return out

    out = asyncio.run(go())
    assert out == {"c-i": ("el", "/model"), "right": ("el", "/model")}


def test_tui_tab_binding_applies_to_slash_lines_only():
    import asyncio

    async def go():
        tui = _tui()
        flt = _binding(tui, "c-i").filter
        tui._input_buffer.insert_text("model")
        prose = bool(flt())
        tui._input_buffer.reset()
        tui._input_buffer.insert_text("/m")
        slash = bool(flt())
        await asyncio.sleep(0.05)
        return prose, slash

    assert asyncio.run(go()) == (False, True)
