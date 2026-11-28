# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A node may be upgraded ahead of the web app that talks to it.

A site pins its own web version, so the node and the browser move
independently and in either order. Every frame this provider emits must
therefore be ADDITIVE: a client that knows only the original keys keeps
working, and a client that knows the new ones degrades to the original
behaviour against an older node.

This is a contract test, not a description. It failed once already: the tool
outcome was first shipped by RESHAPING ``tool_result`` from a string to an
object, which would have rendered a tool name as "[object Object]" on any
client built before the change.
"""

from __future__ import annotations

import json
from typing import Any

from axiom.extensions.builtins.chat.providers.sse_render import SseRenderProvider

#: The frame keys a client built before this work already understood, and the
#: type each one carried. Changing a type here is a breaking wire change and
#: has to be a deliberate, separately-reasoned act.
ORIGINAL_CONTRACT: dict[str, type] = {
    "chunk": str,
    "tool_call": str,
    "tool_result": str,
    "action_result": dict,
    "clarification": dict,
}


def _all_frames() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    p = SseRenderProvider(out.append)
    p.stream_text(iter([]))
    p.render_tool_start("t", {"secret": "x"})
    p.render_tool_result("t", {"ok": True}, 0.1)
    p.render_tool_result("t", {"error": "boom"}, 0.1)
    p.render_message("assistant", "hello")
    p.render_status("m", 1, 2, 0.5, tier="deep")
    p.render_thinking("reasoning", collapsed=True)
    p.render_citations(
        [
            type(
                "C",
                (),
                {
                    "citation_key": "C1", "rank": 1, "source_path": "/a.md",
                    "source_title": "A", "corpus": "rag-org",
                },
            )()
        ]
    )
    return out


class TestOriginalKeysKeepTheirOriginalTypes:
    def test_no_original_key_changed_shape(self):
        for frame in _all_frames():
            for key, expected in ORIGINAL_CONTRACT.items():
                if key in frame:
                    assert isinstance(frame[key], expected), (
                        f"{key!r} changed from {expected.__name__} to "
                        f"{type(frame[key]).__name__} — a client built before "
                        f"this change would misread it"
                    )

    def test_tool_result_is_still_a_name_string(self):
        """The specific regression this file exists for."""
        frames = [f for f in _all_frames() if "tool_result" in f]
        assert frames
        for f in frames:
            assert isinstance(f["tool_result"], str)


class TestEveryFrameIsSerialisable:
    def test_all_frames_round_trip_through_json(self):
        for frame in _all_frames():
            assert json.loads(json.dumps(frame)) == frame


class TestNewKeysAreAdditionsNotReplacements:
    def test_the_outcome_never_travels_without_the_name(self):
        for f in _all_frames():
            if "tool_outcome" in f:
                assert "tool_result" in f, (
                    "tool_outcome replaced tool_result instead of riding "
                    "alongside it; an older client sees no tool at all"
                )

    def test_the_type_guard_can_actually_fail(self):
        """Negative control.

        The check above passes today; this proves it would NOT pass if someone
        reshaped an original key. Without this, a refactor that broke the
        contract and also broke the checking logic would look green.
        """
        import pytest

        violation = {"tool_result": {"name": "t", "ok": True}}  # the old regression
        with pytest.raises(AssertionError):
            for key, expected in ORIGINAL_CONTRACT.items():
                if key in violation:
                    assert isinstance(violation[key], expected), (
                        f"{key!r} changed shape"
                    )


class TestTheClientContractIsDocumentedWhereItIsEnforced:
    def test_the_original_contract_lists_every_pre_existing_key(self):
        """Guards the guard: if someone adds a key here without thinking, the
        test above starts silently checking more than it should."""
        assert set(ORIGINAL_CONTRACT) == {
            "chunk", "tool_call", "tool_result", "action_result", "clarification",
        }
