# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.infra.text_utils — pluralize() and bar() helpers."""

from __future__ import annotations

import pytest

from axiom.infra.text_utils import bar, pluralize
from axiom.setup.renderer import set_color_enabled


@pytest.fixture(autouse=True)
def _reset_color():
    set_color_enabled(False)
    yield
    set_color_enabled(False)


class TestPluralize:
    def test_zero_uses_plural(self):
        assert pluralize(0, "message") == "0 messages"

    def test_one_uses_singular(self):
        assert pluralize(1, "message") == "1 message"

    def test_two_uses_plural(self):
        assert pluralize(2, "message") == "2 messages"

    def test_large_count_uses_plural(self):
        assert pluralize(100, "message") == "100 messages"

    def test_irregular_plural_explicit(self):
        assert pluralize(2, "child", "children") == "2 children"

    def test_irregular_plural_singular_stays_singular(self):
        assert pluralize(1, "child", "children") == "1 child"

    def test_explicit_same_plural(self):
        # fish → fish (same singular and plural)
        assert pluralize(1, "fish", "fish") == "1 fish"
        assert pluralize(3, "fish", "fish") == "3 fish"

    def test_negative_one_is_plural(self):
        # only count == 1 gets singular; anything else including -1 gets plural
        assert pluralize(-1, "step") == "-1 steps"


class TestBar:
    def test_full_bar(self):
        result = bar(100.0, width=20)
        assert result == "█" * 20

    def test_empty_bar(self):
        result = bar(0.0, width=20)
        assert result == "░" * 20

    def test_half_bar(self):
        result = bar(50.0, width=20)
        filled = int(50.0 * 20 / 100)
        assert result == "█" * filled + "░" * (20 - filled)

    def test_width_parameter(self):
        result = bar(50.0, width=10)
        assert len(result) == 10

    def test_over_100_clamps_to_full(self):
        result = bar(150.0, width=20)
        assert result == "█" * 20

    def test_negative_clamps_to_empty(self):
        result = bar(-10.0, width=20)
        assert result == "░" * 20


# ---------------------------------------------------------------------------
# T4.1 — gutter()
# ---------------------------------------------------------------------------


class TestGutter:
    def test_gutter_char_constant_is_cherenkov_block(self):
        from axiom.infra.text_utils import GUTTER_CHAR

        assert GUTTER_CHAR == "▎"

    def test_gutter_prefixes_with_block_and_space_no_color(self):
        from axiom.infra.text_utils import gutter

        result = gutter("hello")
        assert result.startswith("▎ ")
        assert "hello" in result

    def test_gutter_text_uses_cherenkov_block_char(self):
        from axiom.infra.text_utils import gutter

        assert "▎" in gutter("test")

    def test_gutter_uses_cherenkov_color_when_enabled(self):
        from axiom.infra.text_utils import gutter

        set_color_enabled(True)
        result = gutter("hello")
        # ACCENT_BLUE = \033[38;2;0;207;255m
        assert "\033[38;2;0;207;255m" in result
        assert "▎" in result

    def test_gutter_custom_color_applied(self):
        from axiom.infra.text_utils import gutter

        set_color_enabled(True)
        result = gutter("hello", color="\033[32m")
        assert "\033[32m" in result
        assert "▎" in result


# ---------------------------------------------------------------------------
# T4.2 — header()
# ---------------------------------------------------------------------------


class TestHeader:
    def test_header_strips_trailing_colon(self):
        from axiom.infra.text_utils import header

        result = header("Health Check:")
        assert ":" not in result

    def test_header_sentence_cases_multi_word(self):
        from axiom.infra.text_utils import header

        # color off — just check the text
        result = header("Health Check")
        assert "health check" in result.lower()
        # first word still capitalized
        assert "Health" in result

    def test_header_single_word_unchanged_case(self):
        from axiom.infra.text_utils import header

        result = header("Tokens")
        assert "Tokens" in result

    def test_header_preserves_proper_nouns(self):
        from axiom.infra.text_utils import header

        result = header("Available Anthropic providers")
        assert "Anthropic" in result

    def test_header_preserves_axi_proper_noun(self):
        from axiom.infra.text_utils import header

        result = header("Axi banner")
        assert "Axi" in result

    def test_header_wraps_in_bold_when_color(self):
        from axiom.infra.text_utils import header

        set_color_enabled(True)
        result = header("Tokens")
        assert "\033[1m" in result


# ---------------------------------------------------------------------------
# T4.3 — surface_block()
# ---------------------------------------------------------------------------


class TestSurfaceBlock:
    def test_surface_block_has_one_leading_blank(self):
        from axiom.infra.text_utils import surface_block

        result = surface_block(["line one", "line two"])
        assert result.startswith("\n")
        assert result[1] != "\n"  # exactly one leading blank

    def test_surface_block_has_zero_trailing_blanks(self):
        from axiom.infra.text_utils import surface_block

        result = surface_block(["line one", "line two"])
        assert not result.endswith("\n")

    def test_surface_block_joins_lines(self):
        from axiom.infra.text_utils import surface_block

        result = surface_block(["a", "b", "c"])
        assert "a" in result
        assert "b" in result
        assert "c" in result

    def test_surface_block_empty_lines_list(self):
        from axiom.infra.text_utils import surface_block

        result = surface_block([])
        assert result == "\n"


# ---------------------------------------------------------------------------
# T4.8 — code_block_lines()
# ---------------------------------------------------------------------------


class TestCodeBlockLines:
    def test_code_block_header_contains_lang(self):
        from axiom.infra.text_utils import code_block_lines

        lines = code_block_lines("x = 1", lang="python")
        header_line = lines[0]
        assert "python" in header_line

    def test_code_block_body_has_left_rule(self):
        from axiom.infra.text_utils import code_block_lines

        lines = code_block_lines("x = 1\ny = 2", lang="python")
        body_lines = lines[1:]
        for ln in body_lines:
            if ln.strip():
                assert "│" in ln

    def test_code_block_default_lang_label(self):
        from axiom.infra.text_utils import code_block_lines

        lines = code_block_lines("echo hi")
        assert "code" in lines[0]

    def test_code_block_returns_list(self):
        from axiom.infra.text_utils import code_block_lines

        result = code_block_lines("x = 1")
        assert isinstance(result, list)
        assert len(result) >= 2  # header + at least one body line
