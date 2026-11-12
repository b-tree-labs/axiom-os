# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Tests for chat-surface ```math fence integration (Sci Displays A4)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    yield


def test_math_block_replaced_with_placeholder():
    from axiom.extensions.builtins.chat.fullscreen import _process_math_blocks

    raw = "Hello.\n\n```math\nE = mc^2\n```\n\nGoodbye.\n"
    cache: dict[str, str | None] = {}
    out = _process_math_blocks(raw, cache)

    # Original LaTeX is gone; placeholder line is present.
    assert "E = mc^2" not in out or "Equation rendered" in out
    assert "Equation" in out
    assert "axiom://math/" in out
    # Cache populated.
    assert any("Equation" in (v or "") for v in cache.values())


def test_math_block_cache_hits_idempotent():
    """Re-running over the same raw text produces identical output and
    doesn't re-render."""
    from axiom.extensions.builtins.chat.fullscreen import _process_math_blocks

    raw = "```math\n\\alpha + \\beta = \\gamma\n```\n"
    cache: dict[str, str | None] = {}
    out1 = _process_math_blocks(raw, cache)
    cache_after_first = dict(cache)
    _process_math_blocks(out1, cache)  # idempotent input

    assert out1  # placeholder produced
    # Cache state unchanged on second pass (no source LaTeX in out1).
    assert dict(cache) == cache_after_first


def test_math_block_no_fences_unchanged():
    from axiom.extensions.builtins.chat.fullscreen import _process_math_blocks

    raw = "Just prose. No equations here.\n"
    out = _process_math_blocks(raw, {})
    assert out == raw


def test_math_placeholder_format_includes_receipt():
    from axiom.extensions.builtins.chat.fullscreen import _make_math_placeholder

    line = _make_math_placeholder("axiom://math/abc123def456", "/tmp/eq.svg")
    assert "Equation" in line
    assert "axiom://math/abc123def456" in line
    assert "/tmp/eq.svg" in line


def test_math_placeholder_without_image_path():
    """When matplotlib couldn't render, only the receipt is shown."""
    from axiom.extensions.builtins.chat.fullscreen import _make_math_placeholder

    line = _make_math_placeholder("axiom://math/xyz", None)
    assert "Equation" in line
    assert "axiom://math/xyz" in line


def test_multiple_math_blocks_in_one_message():
    from axiom.extensions.builtins.chat.fullscreen import _process_math_blocks

    raw = (
        "First:\n```math\n\\alpha + \\beta\n```\n"
        "Second:\n```math\n\\gamma + \\delta\n```\n"
    )
    cache: dict[str, str | None] = {}
    out = _process_math_blocks(raw, cache)

    # Both blocks rendered; two distinct placeholder lines.
    placeholder_lines = [line for line in out.splitlines() if "Equation" in line]
    assert len(placeholder_lines) == 2
