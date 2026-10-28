# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Reasoning reaches the browser, collapsed by default.

The agent already calls ``render_thinking`` on every turn that produces
reasoning — unlike ``render_status``, which had a single terminal caller. The
web provider was simply stubbed, so the reasoning was computed, handed to a
renderer, and thrown away.

The terminal collapses to three lines with an "expand" hint because a console
cannot do better. A browser can, so the frame carries the FULL text plus the
``collapsed`` preference and lets the surface build a real disclosure control
rather than shipping a pre-truncated string nothing can expand.
"""

from __future__ import annotations


def _frames(text: str, collapsed: bool = True):
    from axiom.extensions.builtins.chat.providers.sse_render import SseRenderProvider

    out: list[dict] = []
    SseRenderProvider(out.append).render_thinking(text, collapsed)
    return out


class TestReasoningReachesTheWire:
    def test_a_thinking_frame_is_emitted(self):
        [f] = _frames("step one\nstep two")
        assert "thinking" in f

    def test_the_full_text_travels_not_a_truncated_preview(self):
        body = "\n".join(f"line {i}" for i in range(20))
        [f] = _frames(body)
        assert f["thinking"]["text"] == body
        assert "line 19" in f["thinking"]["text"]

    def test_the_collapsed_preference_travels(self):
        assert _frames("x", True)[0]["thinking"]["collapsed"] is True
        assert _frames("x", False)[0]["thinking"]["collapsed"] is False

    def test_empty_reasoning_emits_nothing(self):
        assert _frames("") == []

    def test_whitespace_only_reasoning_emits_nothing(self):
        """An empty disclosure widget is worse than no widget."""
        assert _frames("   \n  \n") == []

    def test_the_frame_survives_json(self):
        import json

        [f] = _frames("a\nb")
        assert json.loads(json.dumps(f))["thinking"]["text"] == "a\nb"
