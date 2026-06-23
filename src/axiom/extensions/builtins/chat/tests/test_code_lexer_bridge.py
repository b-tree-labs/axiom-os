# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Tests for the chat-surface bridge from Pygments tokens → prompt_toolkit
class names. This is the in-line code-rendering surface (ADR-039 D12 spec
§6b) for fenced code blocks emitted in agent responses.

The bridge:
  - Resolves the Pygments lexer for the fenced language (cached).
  - Tokenizes the line; emits ``(class:pygments.<token>, text)`` tuples.
  - Falls back cleanly to ``class:dim`` when the language is unknown or
    tokenization fails.
"""

from __future__ import annotations


def test_python_keyword_emits_pygments_class():
    from axiom.extensions.builtins.chat.fullscreen import _style_code_line

    out = _style_code_line("def foo():", "python")
    classes = [c for c, _ in out]
    assert "class:pygments.keyword" in classes
    assert "class:pygments.name.function" in classes


def test_class_root_is_pygments_not_token():
    """The class-name root must be 'pygments.X' (not 'token.X') so that
    style_from_pygments_cls's registered classes actually match."""
    from axiom.extensions.builtins.chat.fullscreen import _style_code_line

    out = _style_code_line("x = 1", "python")
    for cls, _ in out:
        assert cls.startswith("class:pygments") or cls == "class:dim"


def test_unknown_language_falls_back_to_dim():
    from axiom.extensions.builtins.chat.fullscreen import _style_code_line

    out = _style_code_line("anything", "totally-not-a-language")
    assert out == [("class:dim", "anything")]


def test_string_literal_classified():
    from axiom.extensions.builtins.chat.fullscreen import _style_code_line

    out = _style_code_line('s = "hello"', "python")
    classes = [c for c, _ in out]
    # Pygments' string token may be class:pygments.literal.string.* or
    # class:pygments.literal.string (varies by lexer); accept either.
    assert any(c.startswith("class:pygments.literal.string") for c in classes), classes


def test_number_literal_classified():
    from axiom.extensions.builtins.chat.fullscreen import _style_code_line

    out = _style_code_line("answer = 42", "python")
    classes = [c for c, _ in out]
    assert any("number" in c for c in classes), classes


def test_pygments_lexer_cache_hits():
    """Second call for the same language should hit the cache, not re-resolve."""
    from axiom.extensions.builtins.chat.fullscreen import (
        _PYGMENTS_LEXER_CACHE,
        _resolve_pygments_lexer,
    )

    _PYGMENTS_LEXER_CACHE.clear()
    a = _resolve_pygments_lexer("python")
    b = _resolve_pygments_lexer("python")
    assert a is b
    assert "python" in _PYGMENTS_LEXER_CACHE


def test_typescript_works():
    from axiom.extensions.builtins.chat.fullscreen import _style_code_line

    out = _style_code_line("const x: number = 42;", "ts")
    classes = [c for c, _ in out]
    assert any("keyword" in c for c in classes)


def test_rust_works():
    from axiom.extensions.builtins.chat.fullscreen import _style_code_line

    out = _style_code_line('fn main() { println!("hi"); }', "rust")
    classes = [c for c, _ in out]
    assert "class:pygments.keyword" in classes
