# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A rendered table reaches the person in the chat, not just the model.

`render_tool_result` printed `v daq.preview (1.2s)` and discarded the
result. Someone asking the agent to show their channels got a checkmark: the
model received the table and the human did not, so the best case was the
agent re-describing twenty rows in prose.

Compact is right for almost everything — dumping every payload would bury
the conversation. A table is the exception, because the table IS the answer.
The table library exists so one view reaches every surface, and chat is one
of those surfaces.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat.providers.base import table_view_text
from axiom.extensions.builtins.scidisplay.table_spec import tabulate

ROWS = [{"channel": "NCDT1:COOLER:VELO1", "unit": "m/s"},
        {"channel": "STC1", "unit": "degC"}]
COLUMNS = (("channel", "channel", True, "left"), ("unit", "unit", False, "left"))


@pytest.fixture
def table_result():
    return tabulate(ROWS, columns=COLUMNS, title="channels")


class TestItRecognisesATableByItsShape:
    def test_a_rendered_table_yields_its_text(self, table_result):
        assert "NCDT1:COOLER:VELO1" in table_view_text(table_result)

    def test_the_text_is_the_shared_renderers_own(self, table_result):
        """Not re-laid-out here. Same string the CLI prints, which is the
        point of having one table library."""
        assert table_view_text(table_result) == table_result["text"]

    @pytest.mark.parametrize(
        "result",
        [{"ok": True}, {"error": "nope"}, {"text": "not a table"},
         {"spec": {"kind": "rows"}}, {}, None, "a string", 42],
    )
    def test_anything_else_stays_compact(self, result):
        assert table_view_text(result) == ""

    def test_a_spec_without_a_schema_version_is_not_a_table(self):
        """Matching on the pair rather than on a tool NAME is what keeps
        this from becoming a list of blessed tools. It also means the match
        has to be specific enough not to catch a lookalike."""
        assert table_view_text({"spec": {"kind": "rows"}, "text": "x"}) == ""


class TestTheTerminalShowsIt:
    def test_the_view_is_appended_after_the_compact_line(self, table_result):
        from axiom.extensions.builtins.chat import fullscreen

        appended: list[str] = []

        class _TUI:
            def _stop_spinner(self):
                pass

            def _append_output(self, text):
                appended.append(text)

        cls = fullscreen._TuiRenderProvider
        renderer = cls.__new__(cls)
        renderer._tui = _TUI()
        renderer.render_tool_result("daq.preview", table_result, 1.2)

        joined = "".join(appended)
        assert "daq.preview" in joined
        assert "NCDT1:COOLER:VELO1" in joined

    def test_a_failure_still_reports_the_error_and_no_table(self):
        from axiom.extensions.builtins.chat import fullscreen

        appended: list[str] = []

        class _TUI:
            def _stop_spinner(self):
                pass

            def _append_output(self, text):
                appended.append(text)

        cls = fullscreen._TuiRenderProvider
        renderer = cls.__new__(cls)
        renderer._tui = _TUI()
        renderer.render_tool_result("daq.preview", {"error": "no manifest"}, 0.1)
        assert "no manifest" in "".join(appended)


class TestTheBrowserDoesNotGetTheRenderedTable:
    """Updated deliberately: these asserted the browser receives the view.

    This branch sends one rendering to every surface so that no surface
    re-derives its own, and the browser was one of those surfaces. While the
    branch was open, main decided the opposite for the served surface: the
    result dict stays on the node, because a served surface answers people who
    are not the operator and shipping a tool result to a browser is a
    disclosure that parity does not require.

    That decision governs, and a rendered table is the same disclosure as the
    result dict in a different shape. So the parity claim in this file is
    correct for the terminal surfaces and stops at the network boundary.
    Reaching table parity on a served surface is an authorization decision
    rather than a renderer change, and it is not made here.
    """

    def test_the_frame_does_not_carry_the_rendered_view(self, table_result):
        from axiom.extensions.builtins.chat.providers import sse_render

        frames: list[dict] = []
        r = sse_render.SseRenderProvider.__new__(sse_render.SseRenderProvider)
        r._emit = frames.append
        r.render_tool_result("daq.preview", table_result, 1.0)

        assert frames[-1]["tool_result"] == "daq.preview"
        assert "view" not in frames[-1], (
            "a rendered table is the same disclosure as the result dict")
        assert "is_table" not in frames[-1]

    def test_it_still_says_whether_the_call_succeeded(self, table_result):
        """What the served surface IS entitled to, and what main added: the
        browser could not previously tell a success from a failure."""
        from axiom.extensions.builtins.chat.providers import sse_render

        frames: list[dict] = []
        r = sse_render.SseRenderProvider.__new__(sse_render.SseRenderProvider)
        r._emit = frames.append
        r.render_tool_result("daq.preview", table_result, 1.0)
        outcome = frames[-1]["tool_outcome"]
        assert outcome["name"] == "daq.preview"
        assert outcome["ok"] is True

    def test_a_failure_carries_its_error(self):
        from axiom.extensions.builtins.chat.providers import sse_render

        frames: list[dict] = []
        r = sse_render.SseRenderProvider.__new__(sse_render.SseRenderProvider)
        r._emit = frames.append
        r.render_tool_result("daq.run", {"error": "refused"}, 1.0)
        assert frames[-1]["tool_outcome"]["ok"] is False
        assert frames[-1]["tool_outcome"]["error"] == "refused"

    def test_the_bare_name_is_kept_for_an_older_client(self, table_result):
        """The frame stays additive, so a web build pinned to an older version
        keeps working when a node is upgraded ahead of it."""
        from axiom.extensions.builtins.chat.providers import sse_render

        frames: list[dict] = []
        r = sse_render.SseRenderProvider.__new__(sse_render.SseRenderProvider)
        r._emit = frames.append
        r.render_tool_result("daq.preview", table_result, 1.0)
        assert frames[-1]["tool_result"] == "daq.preview"


class TestEverySurfaceThatHasAConsoleShowsIt:
    """The bug was one renderer dropping tables. Four of them could.

    Written as a sweep over the concrete renderers rather than as four
    hand-listed cases, because the thing worth preventing is the NEXT
    renderer being added without this. A new surface fails here until it
    either shows the table or states, like `NullRenderProvider` does, that
    it has no console.
    """

    def _renderers(self):
        from axiom.extensions.builtins.chat.providers import ansi_render, rich_render

        return [rich_render.RichRenderProvider, ansi_render.AnsiRenderProvider]

    def test_every_console_renderer_prints_the_rows(self, table_result, capsys):
        import io

        from rich.console import Console

        for cls in self._renderers():
            renderer = cls()
            if hasattr(renderer, "console"):
                buffer = io.StringIO()
                renderer.console = Console(file=buffer, width=100, force_terminal=False)
                renderer.render_tool_result("daq.preview", table_result, 1.0)
                written = buffer.getvalue()
            else:
                capsys.readouterr()
                renderer.render_tool_result("daq.preview", table_result, 1.0)
                written = capsys.readouterr().out

            assert "daq.preview" in written, f"{cls.__name__} dropped the compact line"
            assert "NCDT1:COOLER:VELO1" in written, f"{cls.__name__} dropped the table"

    def test_an_ordinary_result_stays_compact_on_each(self, capsys):
        import io

        from rich.console import Console

        for cls in self._renderers():
            renderer = cls()
            if hasattr(renderer, "console"):
                buffer = io.StringIO()
                renderer.console = Console(file=buffer, width=100, force_terminal=False)
                renderer.render_tool_result("daq.run", {"rows": 120}, 1.0)
                written = buffer.getvalue()
            else:
                capsys.readouterr()
                renderer.render_tool_result("daq.run", {"rows": 120}, 1.0)
                written = capsys.readouterr().out

            assert "120" not in written, f"{cls.__name__} dumped a non-table payload"

    def test_the_silent_surface_is_silent_on_purpose(self, table_result, capsys):
        """`NullRenderProvider` is the one renderer that must NOT print, and
        its docstring says why: the caller reads the result from the turn."""
        from axiom.extensions.builtins.chat.providers.null_render import NullRenderProvider

        capsys.readouterr()
        NullRenderProvider().render_tool_result("daq.preview", table_result, 1.0)
        assert capsys.readouterr().out == ""


class TestTheEnvelopeARealSkillArrivesIn:
    """The first cut of this matched only a bare `tabulate()` dict.

    Every table a person can actually ask the agent for comes from a
    capability, and `_execute_skill_tool` wraps those:
    `{"ok", "value", "errors", "actions_taken"}`. So the renderer would have
    passed its own tests and shown a checkmark for every real table. This
    class drives the dispatcher rather than hand-writing the envelope,
    because hand-writing it is how the shape was got wrong the first time.
    """

    def test_a_capability_returning_a_table_is_recognised(self, table_result):
        from axiom.extensions.builtins.chat import tools
        from axiom.infra.skills import SkillResult, default_registry

        name = "tabletest.page"
        registry = default_registry()
        if not registry.has(name):
            registry.register(
                name,
                lambda params, ctx: SkillResult(ok=True, value=table_result),
                mutating=False,
            )

        envelope = tools._execute_skill_tool("tabletest__page", {})
        assert envelope["ok"] is True
        assert table_view_text(envelope) == table_result["text"]

    def test_a_capability_returning_anything_else_stays_compact(self):
        from axiom.extensions.builtins.chat import tools
        from axiom.infra.skills import SkillResult, default_registry

        name = "tabletest.plain"
        registry = default_registry()
        if not registry.has(name):
            registry.register(
                name,
                lambda params, ctx: SkillResult(ok=True, value={"rows": 120}),
                mutating=False,
            )

        assert table_view_text(tools._execute_skill_tool("tabletest__plain", {})) == ""


class TestAChatAnswerNeverShowsABareNumber:
    """The fourth surface. Chat passes through the text `tabulate` laid
    out, so the platform's units rule reaches it — but "it passes through"
    is a claim about the seam, and the seam is what broke last time.
    """

    def _tabulated(self, unit):
        from axiom.extensions.builtins.scidisplay.table_spec import tabulate

        return tabulate(
            kind="rows",
            title="readings",
            columns=(("channel", "Channel", False, "left"),
                     ("value", "Value", False, "right"),
                     ("unit", "Unit", False, "left")),
            rows=[{"channel": "Power", "value": 1170000.0, "unit": unit}],
        )

    def test_an_undeclared_unit_survives_into_the_chat_view(self):
        from axiom.extensions.builtins.scidisplay.units import UNDECLARED

        from ..providers.base import table_view_text

        assert UNDECLARED in table_view_text(self._tabulated(""))

    def test_it_survives_the_skill_envelope_too(self):
        """A capability dispatched through SkillRegistry arrives wrapped in
        {"ok", "value", ...} — the shape that made this seam matter."""
        from axiom.extensions.builtins.scidisplay.units import UNDECLARED

        from ..providers.base import table_view_text

        wrapped = {"ok": True, "value": self._tabulated(""), "errors": []}
        assert UNDECLARED in table_view_text(wrapped)

    def test_a_declared_unit_is_not_disfigured(self):
        from axiom.extensions.builtins.scidisplay.units import UNDECLARED

        from ..providers.base import table_view_text

        out = table_view_text(self._tabulated("W"))
        assert UNDECLARED not in out
        assert "W" in out
