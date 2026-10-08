# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Terminal capability detection for math + chart inline image display.

Per Sci Displays spec §6.1: render path is chosen from the terminal's
declared image-protocol support. Detection is per-session and cached
(image-protocol support doesn't change mid-session).

Resolution:
  - iTerm2          — TERM_PROGRAM=iTerm.app
  - Kitty           — TERM=xterm-kitty (also TERM_PROGRAM=ghostty since
                      Ghostty implements Kitty's protocol)
  - WezTerm         — TERM_PROGRAM=WezTerm
  - Ghostty         — TERM_PROGRAM=ghostty
  - Sixel           — left as ``"none"`` for now (DA1/DA2 query is
                      synchronous with the tty and breaks tests; a
                      runtime probe lives behind ``probe_sixel()``)
  - Plain VT100 / dumb — ``"none"``

The result drives the math-render path: image-protocol-capable
terminals get inline SVG/PNG; everything else gets the
``sympy.printing.pretty`` Unicode multi-line fallback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

ImageProtocol = Literal["iterm2", "kitty", "wezterm", "ghostty", "sixel", "none"]


@dataclass(frozen=True)
class TerminalCapability:
    image_protocol: ImageProtocol
    is_tty: bool
    color_truecolor: bool

    @property
    def supports_inline_image(self) -> bool:
        return self.image_protocol != "none"


def _env(name: str) -> str:
    return (os.environ.get(name, "") or "").strip()


def _detect_image_protocol(env: dict[str, str] | None = None) -> ImageProtocol:
    """Pure function — easy to unit-test by passing a dict."""
    e = env if env is not None else os.environ

    term_program = (e.get("TERM_PROGRAM", "") or "").strip().lower()
    term = (e.get("TERM", "") or "").strip().lower()
    lc_terminal = (e.get("LC_TERMINAL", "") or "").strip().lower()

    # Ghostty (which implements Kitty's image protocol) — detect first
    # because it also sets TERM=xterm-kitty in some configurations.
    if term_program == "ghostty":
        return "ghostty"

    # Kitty proper
    if term == "xterm-kitty" or "kitty" in term_program:
        return "kitty"

    # WezTerm
    if term_program == "wezterm" or "wezterm" in lc_terminal:
        return "wezterm"

    # iTerm2 — distinct from Apple Terminal (which has no image protocol)
    if term_program == "iterm.app" or "iterm" in lc_terminal:
        return "iterm2"

    return "none"


def _detect_truecolor(env: dict[str, str] | None = None) -> bool:
    e = env if env is not None else os.environ
    colorterm = (e.get("COLORTERM", "") or "").strip().lower()
    return colorterm in {"truecolor", "24bit"}


def detect_capability(env: dict[str, str] | None = None, tty: bool | None = None) -> TerminalCapability:
    """Detect the active terminal's image-protocol + truecolor support.

    ``env`` and ``tty`` are injectable for tests. Real callers usually
    pass nothing and let it read ``os.environ`` + ``sys.stdout.isatty()``.
    """
    if tty is None:
        try:
            import sys

            tty = sys.stdout.isatty()
        except Exception:
            tty = False
    return TerminalCapability(
        image_protocol=_detect_image_protocol(env),
        is_tty=bool(tty),
        color_truecolor=_detect_truecolor(env),
    )


__all__ = [
    "ImageProtocol",
    "TerminalCapability",
    "detect_capability",
]
