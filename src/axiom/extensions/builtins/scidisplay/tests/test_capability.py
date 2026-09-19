# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for terminal capability detection (Sci Displays spec §6.1)."""

from __future__ import annotations


def test_iterm2_detected():
    from axiom.extensions.builtins.scidisplay.capability import detect_capability

    cap = detect_capability(env={"TERM_PROGRAM": "iTerm.app", "TERM": "xterm-256color"})
    assert cap.image_protocol == "iterm2"
    assert cap.supports_inline_image


def test_kitty_detected_via_term():
    from axiom.extensions.builtins.scidisplay.capability import detect_capability

    cap = detect_capability(env={"TERM": "xterm-kitty"})
    assert cap.image_protocol == "kitty"


def test_wezterm_detected():
    from axiom.extensions.builtins.scidisplay.capability import detect_capability

    cap = detect_capability(env={"TERM_PROGRAM": "WezTerm"})
    assert cap.image_protocol == "wezterm"


def test_ghostty_detected_even_when_term_is_kitty():
    """Ghostty often sets TERM=xterm-kitty (it implements Kitty's protocol).
    TERM_PROGRAM=ghostty should win over the TERM-based heuristic."""
    from axiom.extensions.builtins.scidisplay.capability import detect_capability

    cap = detect_capability(
        env={"TERM_PROGRAM": "ghostty", "TERM": "xterm-kitty"}
    )
    assert cap.image_protocol == "ghostty"


def test_apple_terminal_no_image_protocol():
    """Apple's stock Terminal.app doesn't speak any image protocol."""
    from axiom.extensions.builtins.scidisplay.capability import detect_capability

    cap = detect_capability(
        env={"TERM_PROGRAM": "Apple_Terminal", "TERM": "xterm-256color"}
    )
    assert cap.image_protocol == "none"
    assert not cap.supports_inline_image


def test_dumb_terminal_no_image_protocol():
    from axiom.extensions.builtins.scidisplay.capability import detect_capability

    cap = detect_capability(env={"TERM": "dumb"})
    assert cap.image_protocol == "none"


def test_truecolor_detection():
    from axiom.extensions.builtins.scidisplay.capability import detect_capability

    cap_tc = detect_capability(env={"COLORTERM": "truecolor"})
    cap_24 = detect_capability(env={"COLORTERM": "24bit"})
    cap_no = detect_capability(env={})
    assert cap_tc.color_truecolor is True
    assert cap_24.color_truecolor is True
    assert cap_no.color_truecolor is False


def test_supports_inline_image_property():
    from axiom.extensions.builtins.scidisplay.capability import TerminalCapability

    yes = TerminalCapability(image_protocol="iterm2", is_tty=True, color_truecolor=True)
    no = TerminalCapability(image_protocol="none", is_tty=True, color_truecolor=True)
    assert yes.supports_inline_image is True
    assert no.supports_inline_image is False


def test_real_environment_doesnt_raise():
    """Reading the real environment should always produce SOME answer
    without raising — even on weird CI runners."""
    from axiom.extensions.builtins.scidisplay.capability import detect_capability

    cap = detect_capability()
    assert cap.image_protocol in {"iterm2", "kitty", "wezterm", "ghostty", "sixel", "none"}
