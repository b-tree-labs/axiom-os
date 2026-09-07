# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the TUI's OS-backed clipboard (D10).

All tool invocations go through a fake runner, so nothing here touches the
real clipboard or needs pbcopy/xclip installed.
"""

from __future__ import annotations

import base64
import subprocess

import pytest
from prompt_toolkit.clipboard import ClipboardData
from prompt_toolkit.selection import SelectionType

from axiom.extensions.builtins.chat.clipboard import (
    SystemClipboard,
    copy_to_system_clipboard,
    read_system_clipboard,
)


class FakeTools:
    """A runner standing in for pbcopy/pbpaste and friends.

    ``paste`` is what a read returns. Tools named in ``missing`` raise
    FileNotFoundError, tools in ``failing`` exit non-zero, and ``raises`` makes
    every call raise that exception. Each call lands in ``calls`` as
    ``(argv, stdin bytes)``; the last successful copy lands in ``copied``.
    """

    def __init__(
        self,
        *,
        paste: bytes = b"",
        missing: frozenset[str] = frozenset(),
        failing: frozenset[str] = frozenset(),
        raises: Exception | None = None,
    ) -> None:
        self.paste = paste
        self.missing = missing
        self.failing = failing
        self.raises = raises
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.copied: bytes | None = None

    def __call__(
        self, cmd: list[str], data: bytes | None, timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((cmd, data))
        if self.raises is not None:
            raise self.raises
        if cmd[0] in self.missing:
            raise FileNotFoundError(cmd[0])
        if cmd[0] in self.failing:
            return subprocess.CompletedProcess(cmd, 1, stdout=b"")
        if data is None:
            return subprocess.CompletedProcess(cmd, 0, stdout=self.paste)
        self.copied = data
        return subprocess.CompletedProcess(cmd, 0, stdout=b"")


def test_get_data_returns_what_pbpaste_returns():
    tools = FakeTools(paste=b"copied outside the app")
    clipboard = SystemClipboard(runner=tools, platform="darwin")

    assert clipboard.get_data().text == "copied outside the app"
    assert tools.calls == [(["pbpaste"], None)]


def test_set_data_reaches_the_system_copier():
    tools = FakeTools()
    clipboard = SystemClipboard(runner=tools, platform="darwin")

    clipboard.set_data(ClipboardData("hello"))

    assert tools.calls == [(["pbcopy"], b"hello")]
    assert tools.copied == b"hello"


def test_set_text_is_the_base_class_shortcut():
    tools = FakeTools()
    clipboard = SystemClipboard(runner=tools, platform="darwin")

    clipboard.set_text("via set_text")

    assert tools.copied == b"via set_text"


@pytest.mark.parametrize(
    "tools",
    [
        pytest.param(FakeTools(missing=frozenset({"pbpaste"})), id="tool missing"),
        pytest.param(FakeTools(failing=frozenset({"pbpaste"})), id="tool exits non-zero"),
        pytest.param(
            FakeTools(raises=subprocess.TimeoutExpired(["pbpaste"], 1.0)), id="tool times out"
        ),
    ],
)
def test_get_data_falls_back_to_the_last_set_data_value(tools: FakeTools):
    clipboard = SystemClipboard(runner=tools, platform="darwin")
    clipboard.set_data(ClipboardData("kept in memory"))

    assert clipboard.get_data().text == "kept in memory"


def test_get_data_is_empty_when_nothing_was_ever_copied_and_no_tool_answers():
    clipboard = SystemClipboard(runner=FakeTools(missing=frozenset({"pbpaste"})), platform="darwin")

    assert clipboard.get_data().text == ""


def test_os_text_matching_the_ring_keeps_its_selection_type():
    tools = FakeTools(paste=b"a line\n")
    clipboard = SystemClipboard(runner=tools, platform="darwin")
    clipboard.set_data(ClipboardData("a line\n", SelectionType.LINES))

    data = clipboard.get_data()

    assert data.text == "a line\n"
    assert data.type is SelectionType.LINES


def test_newer_os_text_wins_over_the_ring():
    tools = FakeTools(paste=b"newer")
    clipboard = SystemClipboard(runner=tools, platform="darwin")
    clipboard.set_data(ClipboardData("older"))

    assert clipboard.get_data().text == "newer"


def test_rotate_walks_the_in_memory_ring():
    tools = FakeTools(missing=frozenset({"pbpaste", "pbcopy"}))
    clipboard = SystemClipboard(runner=tools, platform="darwin")
    clipboard.set_data(ClipboardData("first"))
    clipboard.set_data(ClipboardData("second"))

    clipboard.rotate()

    assert clipboard.get_data().text == "first"


def test_linux_falls_through_from_xclip_to_xsel():
    tools = FakeTools(paste=b"from xsel", missing=frozenset({"xclip"}))
    clipboard = SystemClipboard(runner=tools, platform="linux")

    clipboard.set_data(ClipboardData("out"))
    text = clipboard.get_data().text

    assert text == "from xsel"
    assert [cmd[0] for cmd, _ in tools.calls] == ["xclip", "xsel", "xclip", "xsel"]
    assert tools.copied == b"out"


def test_windows_paste_normalises_crlf():
    tools = FakeTools(paste=b"one\r\ntwo\r\n")

    assert read_system_clipboard(runner=tools, platform="win32") == "one\ntwo\n"
    assert tools.calls[0][0][0] == "powershell"


def test_read_returns_none_when_no_tool_answers():
    tools = FakeTools(missing=frozenset({"xclip", "xsel"}))

    assert read_system_clipboard(runner=tools, platform="linux") is None


def test_copy_falls_back_to_osc52_when_no_tool_takes_it(capsys):
    tools = FakeTools(missing=frozenset({"pbcopy"}))

    taken = copy_to_system_clipboard("hi", runner=tools, platform="darwin")

    assert taken is False
    encoded = base64.b64encode(b"hi").decode("ascii")
    assert f"\033]52;c;{encoded}\a" in capsys.readouterr().out


def test_copy_reports_when_a_tool_took_the_text(capsys):
    tools = FakeTools()

    assert copy_to_system_clipboard("hi", runner=tools, platform="darwin") is True
    assert capsys.readouterr().out == ""
