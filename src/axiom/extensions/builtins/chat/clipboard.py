# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""OS-backed clipboard for the full-screen chat TUI.

prompt_toolkit's default ``InMemoryClipboard`` only ever holds text the
application itself put there, so ctrl+V could never paste anything copied
outside the TUI (defect D10). :class:`SystemClipboard` fronts the operating
system's clipboard through the platform's own tools (``pbcopy``/``pbpaste`` on
macOS, ``xclip``/``xsel`` on X11, ``Get-Clipboard`` on Windows) and keeps an
in-memory kill ring as the fallback for hosts without one, such as a bare SSH
session, where a copy still reaches the terminal through the OSC 52 escape.

Every tool invocation goes through a *runner* callable so tests can inject a
fake instead of touching the real clipboard.
"""

from __future__ import annotations

import base64
import subprocess
import sys
from collections.abc import Callable

from prompt_toolkit.clipboard import Clipboard, ClipboardData, InMemoryClipboard

__all__ = [
    "SystemClipboard",
    "ToolRunner",
    "copy_to_system_clipboard",
    "read_system_clipboard",
    "run_clipboard_tool",
]

#: ``runner(argv, stdin_bytes_or_None, timeout_seconds)`` -> completed process.
#: Any exception it raises means "this tool is unavailable".
ToolRunner = Callable[[list[str], bytes | None, float], subprocess.CompletedProcess[bytes]]

_COPY_TIMEOUT = 2.0  # seconds; the budget the old pbcopy/xclip path already used
_PASTE_TIMEOUT = 1.0  # seconds; a paste blocks the key handler, so keep it short

_X11_COPY: tuple[list[str], ...] = (
    ["xclip", "-selection", "clipboard"],
    ["xsel", "--clipboard", "--input"],
)
_X11_PASTE: tuple[list[str], ...] = (
    ["xclip", "-selection", "clipboard", "-o"],
    ["xsel", "--clipboard", "--output"],
)


def _copy_commands(platform: str) -> tuple[list[str], ...]:
    if platform == "darwin":
        return (["pbcopy"],)
    # Everything else, Windows included, tries the X11 tools and then relies on
    # the OSC 52 escape, exactly as before.
    return _X11_COPY


def _paste_commands(platform: str) -> tuple[list[str], ...]:
    if platform == "darwin":
        return (["pbpaste"],)
    if platform == "win32":
        return (["powershell", "-NoProfile", "-Command", "Get-Clipboard", "-Raw"],)
    return _X11_PASTE


def run_clipboard_tool(
    cmd: list[str], data: bytes | None, timeout: float
) -> subprocess.CompletedProcess[bytes]:
    """Default runner: run *cmd* to completion, feeding *data* on stdin when given.

    Raises whatever :func:`subprocess.run` raises (``FileNotFoundError`` for a
    missing tool, ``TimeoutExpired`` for a wedged one). stdin is detached when
    there is nothing to feed so a paste tool can never block on the TTY.
    """
    if data is None:
        return subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    return subprocess.run(
        cmd,
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    )


def copy_to_system_clipboard(
    text: str,
    *,
    runner: ToolRunner = run_clipboard_tool,
    platform: str = sys.platform,
) -> bool:
    """Best-effort copy of *text* to the OS clipboard.

    Tries the platform tools first, then falls back to the OSC 52 escape
    sequence, which works across SSH and in most modern terminals. Returns
    True when a platform tool took the text, False when only the escape was
    emitted.
    """
    data = text.encode("utf-8")
    for cmd in _copy_commands(platform):
        try:
            if runner(cmd, data, _COPY_TIMEOUT).returncode == 0:
                return True
        except Exception:  # a missing or wedged tool must never break the TUI
            continue

    try:
        encoded = base64.b64encode(data).decode("ascii")
        sys.stdout.write(f"\033]52;c;{encoded}\a")
        sys.stdout.flush()
    except Exception:  # best effort, same as above
        pass
    return False


def read_system_clipboard(
    *,
    runner: ToolRunner = run_clipboard_tool,
    platform: str = sys.platform,
) -> str | None:
    """The OS clipboard's current text, or None when no tool could read it."""
    for cmd in _paste_commands(platform):
        try:
            proc = runner(cmd, None, _PASTE_TIMEOUT)
        except Exception:  # missing tool, no display, timeout: try the next one
            continue
        if proc.returncode == 0:
            text = proc.stdout.decode("utf-8", errors="replace")
            return text.replace("\r\n", "\n") if platform == "win32" else text
    return None


class SystemClipboard(Clipboard):
    """A prompt_toolkit clipboard that reads and writes the OS clipboard.

    ``set_data`` records the text in an in-memory kill ring *and* pushes it to
    the OS. ``get_data`` prefers what the OS holds right now, which is how text
    copied outside the TUI reaches ctrl+V, and falls back to the ring when no
    clipboard tool answers. ``set_text`` and ``rotate`` keep the base-class
    semantics.
    """

    def __init__(
        self,
        *,
        runner: ToolRunner = run_clipboard_tool,
        platform: str = sys.platform,
    ) -> None:
        self._runner = runner
        self._platform = platform
        self._memory = InMemoryClipboard()

    def set_data(self, data: ClipboardData) -> None:
        self._memory.set_data(data)
        copy_to_system_clipboard(data.text, runner=self._runner, platform=self._platform)

    def get_data(self) -> ClipboardData:
        text = read_system_clipboard(runner=self._runner, platform=self._platform)
        latest = self._memory.get_data()
        if text is None or text == latest.text:
            # No tool answered, or the OS still holds our own last copy: the
            # ring entry is the same text and keeps its selection type.
            return latest
        return ClipboardData(text)

    def rotate(self) -> None:
        self._memory.rotate()
