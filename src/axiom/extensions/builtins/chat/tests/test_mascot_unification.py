# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""T4.12 — fullscreen welcome reuses canonical Axi banner from setup.renderer."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText


class TestAxiBannerLines:
    """T4.12 — axi_banner_lines() exposed from setup.renderer."""

    def test_axi_banner_lines_is_callable(self):
        from axiom.setup.renderer import axi_banner_lines
        assert callable(axi_banner_lines)

    def test_axi_banner_lines_returns_nonempty_list(self):
        from axiom.setup.renderer import axi_banner_lines
        lines = axi_banner_lines()
        assert isinstance(lines, list)
        assert len(lines) > 0

    def test_axi_banner_lines_contains_ship_chars(self):
        """Canonical AXI art contains (o) eye pupils and AXI hull placard."""
        from axiom.setup.renderer import axi_banner_lines
        art = "\n".join(axi_banner_lines())
        # Old salamander uses rounded arc corners ╭╮
        assert "╭" not in art, "axi_banner_lines must not return old salamander art"
        # AXI mascot has (o) pupils + AXI placard label
        assert "(o)" in art, "expected AXI eye pupils '(o)'"
        assert "AXI" in art, "expected AXI hull placard"


class TestFullscreenMascotUnification:
    """T4.12 — fullscreen welcome uses canonical banner, not hardcoded salamander."""

    def _make_tui(self):
        from axiom.extensions.builtins.chat.fullscreen import FullScreenChat

        tui = object.__new__(FullScreenChat)
        tui._output_lock = threading.Lock()
        tui._raw_output = ""
        tui._wrapped_committed = ""
        tui._partial_tail = ""
        tui._table_pending = []
        tui._align_table_cache = {}
        tui._last_invalidate = 0.0
        tui._pending_invalidate = False
        tui._spinner_visible = False
        tui._spinner_text = FormattedText([])
        tui._busy = False
        tui._inflight_tokens_in = 0
        tui._inflight_tokens_out = 0
        tui._inflight_started_at = None
        tui._thinking_buffer = ""
        tui._thinking_open = False
        tui._picker = None
        tui._approval_pending = None
        tui._mode_idx = 0
        tui._mode_changed_at = 0.0
        tui._last_model = ""
        tui._last_tokens = ""
        tui._last_cost = ""
        tui._update_info = None
        tui._update_dismissed = False
        tui._new_lines_below = 0
        tui._user_scrolled_up = False

        buf = Buffer(read_only=True, name="output_t412")
        buf.set_document(Document("", 0), bypass_readonly=True)
        tui._output_buffer = buf

        app = MagicMock()
        app.output.get_size.return_value.columns = 80
        tui._app = app
        return tui

    def test_fullscreen_welcome_does_not_use_old_salamander(self):
        """T4.12 — fullscreen provider render_welcome omits old ╭ rounded corners."""
        from unittest.mock import MagicMock

        from axiom.extensions.builtins.chat.fullscreen import _TuiRenderProvider

        tui = self._make_tui()
        provider = object.__new__(_TuiRenderProvider)
        provider._tui = tui
        provider._agent = MagicMock()
        provider._store = MagicMock()

        provider.render_welcome()
        output = tui._output_buffer.text
        # Old salamander uses ╭ (U+256D arc corner) — must be gone
        assert "╭" not in output, (
            "fullscreen welcome must use canonical Axi banner, not old salamander (╭ found)"
        )

    def test_fullscreen_welcome_shows_the_branded_mascot(self):
        """T4.12 — fullscreen render_welcome shows the micro-mascot.

        The placard names whichever agent the operator launched, so it is the
        branding registry's `mascot_name` rather than a hardcoded AXI. Under
        vanilla Axiom that is "Axi"; a consumer layer registering "Neut" gets
        Neut. Asserting the literal AXI here was what let `neut chat` open with
        the platform's persona while the prompt said neut.
        """
        from unittest.mock import MagicMock

        from axiom.extensions.builtins.chat.fullscreen import _TuiRenderProvider

        tui = self._make_tui()
        provider = object.__new__(_TuiRenderProvider)
        provider._tui = tui
        provider._agent = MagicMock()
        provider._store = MagicMock()

        provider.render_welcome()
        output = tui._output_buffer.text
        # Fullscreen uses the unicode micro-mascot: (◉)─(◉) + the placard.
        # Multi-row ASCII art was abandoned because prompt-toolkit shreds
        # it into detached `|` columns (2026-05-04 chat-TUI iteration).
        from axiom.infra.branding import get_branding

        expected = get_branding().mascot_name
        assert expected in output, (
            f"fullscreen welcome must show the branded placard {expected!r}"
        )
        assert "◉" in output, "fullscreen welcome must show the (◉) eye pupils"
        assert "AXI" not in output, (
            "the placard is the mascot's name in title case, not the all-caps "
            "agent persona — `neut chat` must not introduce itself as AXI"
        )
