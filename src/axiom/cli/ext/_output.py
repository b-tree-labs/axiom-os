# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Shared output helpers for ``axi ext`` verbs.

Every verb prints through this module so the surface stays consistent:

- ``status(...)`` — one status row with ``PASS`` / ``FAIL`` / ``WARN`` / ``INFO``.
- ``table(...)`` — titled table (Rich when on a TTY, plain ASCII otherwise).
- ``error(...)`` — standard error envelope to stderr.
- ``next_steps(...)`` — indented hints pointing at the next natural verb.
- ``heading(...)`` — bold underlined section heading.
- ``spinner(...)`` — context manager for long-running ops.

TTY behaviour: Rich renders color + ``✓ ✗ ⚠ •`` glyphs.

Non-TTY behaviour (tests, pipes, CI): the same semantic output, but with
plain bracketed tags (``[PASS]``, ``[FAIL]``, ``[WARN]``, ``[INFO]``) and
ASCII tables so existing substring-style assertions keep matching.

Tests and pipes are detected by ``sys.stdout.isatty()`` plus the ``NO_COLOR``
env var (https://no-color.org). The cached :func:`console` instance is built
with ``force_terminal=False`` when non-TTY so Rich never re-enables ANSI in
captured output.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from rich.console import Console
from rich.table import Table

# ---------------------------------------------------------------------------
# Terminator constants
# ---------------------------------------------------------------------------
#
# Callers emit a final one-liner after a verb's structured output. These
# constants keep the phrasing consistent across verbs ("X: all checks passed."
# vs. "X: 3 checks failed.").

TERMINATOR_OK = "all checks passed."
TERMINATOR_FAIL = "checks failed."
TERMINATOR_WARN = "warnings present."


# ---------------------------------------------------------------------------
# Console factories
# ---------------------------------------------------------------------------
#
# We don't cache the Console instance. Rich prefers a long-lived console, but
# pytest's ``capsys`` reassigns ``sys.stdout`` / ``sys.stderr`` per-test, and
# a cached Console would keep writing to whichever stream was live at first
# call. Building a fresh Console per call is fast enough that the simplicity
# wins here.


def _tty(stream) -> bool:
    try:
        return bool(getattr(stream, "isatty", lambda: False)())
    except ValueError:  # pragma: no cover — closed stream
        return False


def _color_enabled(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("AXIOM_FORCE_PLAIN"):
        return False
    return _tty(stream)


def _build_console(stream) -> Console:
    use_color = _color_enabled(stream)
    return Console(
        file=stream,
        force_terminal=use_color,
        no_color=not use_color,
        highlight=False,
        soft_wrap=True,
    )


def console() -> Console:
    """Return a :class:`Console` bound to the current ``sys.stdout``."""
    return _build_console(sys.stdout)


def error_console() -> Console:
    """Return a :class:`Console` bound to the current ``sys.stderr``."""
    return _build_console(sys.stderr)


def _reset_console_cache() -> None:
    """Retained for backwards-compat with the tests; no-op now."""
    return None


# ---------------------------------------------------------------------------
# Status rows
# ---------------------------------------------------------------------------


_StatusLevel = Literal["pass", "fail", "warn", "info"]


_STATUS_GLYPHS: dict[str, tuple[str, str, str]] = {
    # level -> (tty_mark, plain_tag, rich_style)
    "pass": ("✓", "[PASS]", "green"),
    "fail": ("✗", "[FAIL]", "red"),
    "warn": ("⚠", "[WARN]", "yellow"),
    "info": ("•", "[INFO]", "cyan"),
}


def status(level: _StatusLevel, check: str, detail: str = "") -> None:
    """Print one status row. ``level`` must be one of pass/fail/warn/info."""
    if level not in _STATUS_GLYPHS:
        raise ValueError(f"unknown status level: {level!r}")
    mark, plain_tag, style = _STATUS_GLYPHS[level]
    con = console()
    if _color_enabled(sys.stdout):
        body = f"{check}" if not detail else f"{check}: {detail}"
        con.print(f"  [{style}]{mark}[/{style}] {body}")
    else:
        body = f"{check}" if not detail else f"{check}: {detail}"
        con.print(f"  {plain_tag} {body}")


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def table(title: str, columns: list[str], rows: list[list[str]]) -> None:
    """Print a titled table. Rich when TTY; plain ASCII otherwise."""
    con = console()
    if _color_enabled(sys.stdout):
        t = Table(title=title, show_lines=False, header_style="bold")
        for col in columns:
            t.add_column(col)
        for row in rows:
            t.add_row(*(str(cell) for cell in row))
        con.print(t)
        return

    # Plain-text fallback — stable column widths, header separator.
    if not rows:
        if title:
            con.print(title)
        con.print("  ".join(columns))
        return

    widths = [len(c) for c in columns]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    header = "  ".join(columns[i].ljust(widths[i]) for i in range(len(columns)))
    sep = "-" * len(header)
    if title:
        con.print(title)
    con.print(header)
    con.print(sep)
    for row in rows:
        con.print(
            "  ".join(str(row[i]).ljust(widths[i]) for i in range(len(columns)))
        )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def error(message: str, *, hint: str | None = None) -> None:
    """Print a standard error envelope to stderr.

    TTY: ``error:`` is bold red; the optional hint is dimmed.
    Non-TTY: ``error: <message>`` plain, optional ``hint: <hint>`` line.
    """
    con = error_console()
    if _color_enabled(sys.stderr):
        con.print(f"[bold red]error:[/bold red] {message}")
        if hint:
            con.print(f"[dim]hint:[/dim] {hint}")
    else:
        con.print(f"error: {message}")
        if hint:
            con.print(f"hint: {hint}")


# ---------------------------------------------------------------------------
# Next-steps block
# ---------------------------------------------------------------------------


def next_steps(steps: list[str], *, header: str = "Next steps:") -> None:
    """Print an indented hint block pointing at the next natural verb."""
    if not steps:
        return
    con = console()
    if _color_enabled(sys.stdout):
        con.print(f"[bold]{header}[/bold]")
        for step in steps:
            con.print(f"  [cyan]•[/cyan] {step}")
    else:
        con.print(header)
        for step in steps:
            con.print(f"  - {step}")


# ---------------------------------------------------------------------------
# Headings
# ---------------------------------------------------------------------------


def heading(text: str) -> None:
    """Print a bold underlined section heading. Plain text when non-TTY."""
    con = console()
    if _color_enabled(sys.stdout):
        con.print(f"[bold underline]{text}[/bold underline]")
    else:
        con.print(text)


# ---------------------------------------------------------------------------
# Spinner
# ---------------------------------------------------------------------------


@contextmanager
def spinner(message: str) -> Iterator[None]:
    """Context manager for long-running ops.

    TTY: Rich spinner with the supplied message.
    Non-TTY: prints ``message`` once and yields.
    """
    con = console()
    if _color_enabled(sys.stdout):
        with con.status(message):
            yield
    else:
        con.print(message)
        yield


__all__ = [
    "TERMINATOR_FAIL",
    "TERMINATOR_OK",
    "TERMINATOR_WARN",
    "console",
    "error",
    "error_console",
    "heading",
    "next_steps",
    "spinner",
    "status",
    "table",
]
