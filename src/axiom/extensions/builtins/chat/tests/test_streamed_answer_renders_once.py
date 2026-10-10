# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A streamed answer is drawn once, legibly, on a narrow non-UTF-8 console.

A new user on a Windows laptop saw his first answer like this::

    I'm part of the Axiom platform â an AI assistant ...
    I'm part of the Axiom platform â an AI assistant ...
    I'm part of the Axiom platform â an AI assistant ...
    (five times)

Two defects stacked. The stream decoder turned an em dash into "â" plus two
C1 control characters (fixed in the gateway). Then the live renderer measured
those controls as zero cells wide while the console drew each as a glyph, so
the line wrapped one row earlier than the renderer believed. Every refresh
moved the cursor up one row too few, and each frame left its predecessor on
screen.

The second half is the renderer's to own whatever the source of the text:
nothing it is handed may make its idea of a line's width disagree with the
terminal's. These tests drive the real renderer into a small terminal model
that wraps at a fixed width and draws C1 controls as one cell, the way the
Windows console does, and count what is left on the screen.
"""

from __future__ import annotations

import io
import re

import pytest

pytest.importorskip("rich")

from axiom.extensions.builtins.chat.providers.rich_render import (  # noqa: E402
    RichRenderProvider,
)
from axiom.infra.gateway import StreamChunk  # noqa: E402

_CSI = re.compile(r"\x1b\[([?0-9;]*)([A-Za-z])")


def _screen(raw: str, width: int) -> list[str]:
    """What a terminal `width` columns wide shows after drawing `raw`.

    Enough of a VT100 for what the live renderer emits: carriage return,
    newline, cursor up, erase line; SGR and mode switches are ignored. Every
    other character, C1 controls included, occupies one cell.
    """
    rows: list[list[str]] = [[]]
    row = col = 0
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\x1b":
            m = _CSI.match(raw, i)
            if m:
                params, final = m.group(1), m.group(2)
                n = int(params) if params.isdigit() else 1
                if final == "A":
                    row = max(0, row - n)
                elif final == "K":
                    while len(rows) <= row:
                        rows.append([])
                    rows[row] = []
                i = m.end()
                continue
        if ch == "\r":
            col = 0
        elif ch == "\n":
            row, col = row + 1, 0
        else:
            if col >= width:
                row, col = row + 1, 0
            while len(rows) <= row:
                rows.append([])
            line = rows[row]
            line.extend(" " * (col + 1 - len(line)))
            line[col] = ch
            col += 1
        i += 1
    return ["".join(r).rstrip() for r in rows]


def _stream(provider: RichRenderProvider, text: str) -> str:
    chunks = [StreamChunk(type="text", text=w + " ") for w in text.split(" ")]
    chunks.append(StreamChunk(type="done"))

    def paced():
        import time

        for chunk in chunks:
            time.sleep(0.07)  # slower than the 15 Hz refresh, as a local model is
            yield chunk

    return provider.stream_text(paced())


def _provider(file, monkeypatch, columns: int) -> RichRenderProvider:
    monkeypatch.setenv("COLUMNS", str(columns))
    return RichRenderProvider(file=file, force_terminal=True)


ANSWER_WITH_CONTROLS = "I'm part of the platform â\x80\x94 an AI assistant that helps you."


def test_a_line_with_control_characters_is_drawn_once(monkeypatch):
    buf = io.StringIO()
    provider = _provider(buf, monkeypatch, 40)
    _stream(provider, ANSWER_WITH_CONTROLS)
    screen = _screen(buf.getvalue(), 40)
    starts = [line for line in screen if line.startswith("I'm part of the platform")]
    assert len(starts) == 1, "\n".join(screen)


def test_a_narrowed_terminal_does_not_repeat_the_line(monkeypatch):
    """The width was fixed when chat started; a window narrowed since then
    wrapped every long line and left a copy per refresh."""
    buf = io.StringIO()
    provider = _provider(buf, monkeypatch, 100)
    monkeypatch.setenv("COLUMNS", "40")
    _stream(provider, "I'm part of the platform, an AI assistant that helps you find things.")
    screen = _screen(buf.getvalue(), 40)
    starts = [line for line in screen if line.startswith("I'm part of the platform")]
    assert len(starts) == 1, "\n".join(screen)


def test_a_cp1252_console_gets_the_answer_not_an_exception(monkeypatch):
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True)
    provider = _provider(console, monkeypatch, 60)
    answer = provider.stream_text(
        iter(
            [
                StreamChunk(type="text", text="Power 950 kW → steady — ok ✓"),
                StreamChunk(type="done"),
            ]
        )
    )
    console.flush()
    shown = raw.getvalue().decode("cp1252")
    assert "Power 950 kW" in shown
    assert "steady" in shown
    assert "—" in answer  # the transcript keeps the real text


def test_chat_output_streams_replace_what_they_cannot_encode():
    """Every other line chat prints (tool glyphs, the plain renderer, notices)
    goes straight to stdout. On a cp1252 stream the first arrow raised
    UnicodeEncodeError and ended the session."""
    from axiom.extensions.builtins.chat.cli import tolerate_unencodable_output

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True)
    tolerate_unencodable_output([stream])
    stream.write("ok → ✗ done\n")
    assert raw.getvalue().decode("cp1252").startswith("ok ? ? done")
