# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Tiny interactive list picker for CLI prompts.

A numbered list the user can arrow-navigate (↑/↓) and select with Enter, or
jump to by typing the number. Degrades cleanly:

- POSIX TTY  → raw-mode arrow + number picker.
- Other TTY (Windows) → numbered prompt, type a number.
- No TTY (pipe/CI)    → prints the list, returns the default without blocking.

``select_index`` returns the chosen 0-based index, the default when accepted or
non-interactive, or ``None`` if cancelled (q / Ctrl-C / Esc).
"""
from __future__ import annotations

import sys

__all__ = ["select_index"]


def _render(title: str, options: list[str], default: int) -> None:
    print(title)
    for i, opt in enumerate(options):
        marker = "→" if i == default else " "
        tag = "  (current)" if i == default else ""
        print(f"  {marker} {i + 1}) {opt}{tag}")


def _select_posix(title: str, options: list[str], default: int) -> int | None:
    import termios
    import tty

    fd = sys.stdin.fileno()
    cur = default
    print(title)
    print("  ↑/↓ to move · Enter to select · or type a number · q to cancel\n")

    def draw(first: bool) -> None:
        if not first:
            # move cursor up to overwrite the previous list
            sys.stdout.write(f"\x1b[{len(options)}A")
        for i, opt in enumerate(options):
            sys.stdout.write("\x1b[2K")  # clear line
            pointer = "\x1b[1m→\x1b[0m" if i == cur else " "
            sys.stdout.write(f"  {pointer} {i + 1}) {opt}\n")
        sys.stdout.flush()

    draw(first=True)
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                return cur
            if ch in ("q", "\x03", "\x1b"):  # q, Ctrl-C; bare Esc handled below
                if ch == "\x1b":
                    # could be an arrow escape sequence: read 2 more
                    nxt = sys.stdin.read(2)
                    if nxt == "[A":
                        cur = (cur - 1) % len(options)
                        draw(first=False)
                        continue
                    if nxt == "[B":
                        cur = (cur + 1) % len(options)
                        draw(first=False)
                        continue
                return None
            if ch.isdigit():
                n = int(ch) - 1
                if 0 <= n < len(options):
                    cur = n
                    draw(first=False)
            # ignore everything else
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\n")
        sys.stdout.flush()


def _select_lineinput(title: str, options: list[str], default: int) -> int | None:
    _render(title, options, default)
    try:
        raw = input(f"  Enter number [1-{len(options)}] (default {default + 1}): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw:
        return default
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return int(raw) - 1
    return default


def select_index(title: str, options: list[str], default: int = 0) -> int | None:
    """Pick one option. Returns its 0-based index, or None if cancelled."""
    if not options:
        return None
    default = max(0, min(default, len(options) - 1))
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        _render(title, options, default)  # non-interactive: show + keep default
        return default
    if sys.platform.startswith("win"):
        return _select_lineinput(title, options, default)
    try:
        return _select_posix(title, options, default)
    except Exception:  # noqa: BLE001 — any TTY weirdness → safe line-input
        return _select_lineinput(title, options, default)
