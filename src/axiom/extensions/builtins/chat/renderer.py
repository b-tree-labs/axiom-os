# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Terminal rendering for chat: streaming, markdown, approval prompts.

Reuses color infrastructure from axiom.setup.renderer and adds
streaming display, basic markdown formatting, and spinners.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from axiom.infra.orchestrator.actions import Action, ActionStatus
from axiom.setup.renderer import _c, _Colors, _use_color

if TYPE_CHECKING:
    from axiom.infra.gateway import StreamChunk


# ---------------------------------------------------------------------------
# Markdown formatting (basic line-level)
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_LIST_RE = re.compile(r"^(\s*)-\s+(.+)$")

# File path patterns for clickable links
# Matches paths like: docs/foo.md, ./tools/bar.py, tools/pipelines/sense/cli.py:123
_FILE_PATH_RE = re.compile(
    r"`([a-zA-Z0-9_./-]+\.[a-zA-Z]+(?::\d+)?)`"  # backticked paths with extension
    r"|"
    r"\b((?:\.?/)?(?:tools|docs|tests|scripts|data|inbox)/[a-zA-Z0-9_./\-]+\.[a-zA-Z]+(?::\d+)?)\b"  # bare repo paths
)

# Repository root for resolving relative paths to absolute
from axiom import REPO_ROOT as _REPO_ROOT  # noqa: E402


def _make_path_clickable(match: re.Match) -> str:
    """Convert a file path match to a clickable terminal link."""
    # Get the matched path (from either capture group)
    path_str = match.group(1) or match.group(2)
    if not path_str:
        return match.group(0)

    # Parse line number if present
    line_num = None
    if ":" in path_str:
        path_str, line_str = path_str.rsplit(":", 1)
        if line_str.isdigit():
            line_num = int(line_str)

    # Resolve to absolute path
    p = Path(path_str)
    if not p.is_absolute():
        p = _REPO_ROOT / p
    p = p.resolve()

    # Only linkify if file exists
    if not p.exists():
        return match.group(0)

    abs_path = str(p)
    if line_num:
        abs_path = f"{abs_path}:{line_num}"

    # Format as clickable link
    display = path_str
    if line_num:
        display = f"{path_str}:{line_num}"

    if not _use_color():
        return abs_path

    # Use OSC 8 hyperlink + plain path fallback
    uri = f"file://{p}"
    # OSC 8: \x1b]8;;URL\x07DISPLAY\x1b]8;;\x07
    hyperlink = f"\x1b]8;;{uri}\x07{_c(_Colors.CYAN, display)}\x1b]8;;\x07"
    return hyperlink


def format_markdown_line(line: str) -> str:
    """Apply basic ANSI formatting to a single markdown line.

    Handles: headings, **bold**, `code`, and - list items.
    Falls back to plain text when color is disabled.
    """
    if not _use_color():
        return line

    # Code block fences — render dim
    if line.strip().startswith("```"):
        return _c(_Colors.DIM, line)

    # Headings
    m = _HEADING_RE.match(line)
    if m:
        return _c(_Colors.BOLD + _Colors.ACCENT_BLUE, line)

    # List items — bullet in cyan
    m = _LIST_RE.match(line)
    if m:
        indent, content = m.group(1), m.group(2)
        content = _apply_inline(content)
        return f"{indent}{_c(_Colors.CYAN, '-')} {content}"

    return _apply_inline(line)


def _apply_inline(text: str) -> str:
    """Apply inline formatting: **bold**, `code`, and clickable file paths."""
    if not _use_color():
        return text

    # First, convert file paths to clickable links (before other formatting)
    text = _FILE_PATH_RE.sub(_make_path_clickable, text)

    # Then apply other inline formatting
    text = _BOLD_RE.sub(lambda m: _c(_Colors.BOLD, m.group(1)), text)
    # Skip inline code formatting for paths that were already linkified (contain OSC 8)
    if "\x1b]8;;" not in text:
        text = _INLINE_CODE_RE.sub(lambda m: _c(_Colors.CYAN, f"`{m.group(1)}`"), text)
    return text


# ---------------------------------------------------------------------------
# Streaming display
# ---------------------------------------------------------------------------


def stream_text(chunks: Iterator[StreamChunk]) -> str:
    """Write streaming text deltas to stdout, return accumulated text.

    Shows partial lines in real-time for responsiveness, then replaces
    with formatted version when the line is complete. Applies markdown
    formatting (headings, bold, lists, code) to complete lines.
    """
    accumulated = ""
    in_code_block = False
    line_buffer = ""  # Buffer for incomplete lines
    partial_displayed = 0  # How many chars of partial line are on screen
    is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

    def _clear_partial():
        """Clear the partially displayed line from screen."""
        nonlocal partial_displayed
        if partial_displayed > 0 and is_tty:
            import shutil

            cols = shutil.get_terminal_size().columns
            # If the partial wrapped onto multiple terminal lines,
            # move the cursor up before clearing.
            extra_lines = partial_displayed // cols
            if extra_lines > 0:
                sys.stdout.write(f"\x1b[{extra_lines}A")
            sys.stdout.write("\r\x1b[J")  # start of line + clear to end of screen
            sys.stdout.flush()
        partial_displayed = 0

    def _show_partial(text: str):
        """Display partial line (unformatted)."""
        nonlocal partial_displayed
        if text:
            indented = "  " + text
            sys.stdout.write(indented)
            sys.stdout.flush()
            partial_displayed = len(indented)

    code_lang = ""  # language token from opening fence

    def _flush_line(line: str):
        """Format and output a complete line."""
        nonlocal in_code_block, code_lang
        _clear_partial()

        # Check for code block toggles
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            if in_code_block:
                # Opening fence — parse language, emit framed header
                code_lang = line.strip().lstrip("`").strip()
                from axiom.infra.text_utils import code_block_lines as _cbl
                header_lines = _cbl("", lang=code_lang)
                sys.stdout.write("  " + header_lines[0] + "\n")
                sys.stdout.flush()
                return
            else:
                code_lang = ""
                return
        elif in_code_block:
            if _use_color():
                rule = _c(_Colors.DIM, "│ ")
            else:
                rule = "│ "
            sys.stdout.write(f"  {rule}{line}\n")
            sys.stdout.flush()
            return
        else:
            # Normal text - apply full markdown formatting
            if _use_color():
                line = format_markdown_line(line)

        sys.stdout.write("  " + line + "\n")
        sys.stdout.flush()

    for chunk in chunks:
        if chunk.type == "text":
            text = chunk.text
            accumulated += text

            # Clear any partial display before processing
            _clear_partial()
            line_buffer += text

            # Process complete lines from buffer
            while "\n" in line_buffer:
                line, line_buffer = line_buffer.split("\n", 1)
                _flush_line(line)

            # Show remaining partial line for responsiveness
            if line_buffer:
                _show_partial(line_buffer)

        elif chunk.type == "tool_use_start":
            # Flush any buffered content first
            if line_buffer:
                _clear_partial()
                _flush_line(line_buffer)
                line_buffer = ""
            msg = f"  {_c(_Colors.DIM, f'[calling {chunk.tool_name}...]')}\n"
            sys.stdout.write(msg)
            sys.stdout.flush()

        elif chunk.type == "tool_use_end":
            pass  # Tool results rendered separately

        elif chunk.type == "done":
            # Flush any remaining buffered content
            _clear_partial()
            if line_buffer:
                if line_buffer.strip().startswith("```"):
                    in_code_block = not in_code_block
                if _use_color() and in_code_block:
                    rule = _c(_Colors.DIM, "│ ")
                    line_buffer = f"{rule}{line_buffer}"
                elif _use_color():
                    line_buffer = format_markdown_line(line_buffer)
                sys.stdout.write("  " + line_buffer)
                line_buffer = ""
            if accumulated and not accumulated.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()
            break

    return accumulated


# ---------------------------------------------------------------------------
# Thinking spinner
# ---------------------------------------------------------------------------


@contextmanager
def render_thinking_spinner(label: str = "Thinking"):
    """Context manager showing a pulse spinner while work happens.

    Yields the spinner object so callers can optionally call
    ``spinner.update_tokens()`` or ``spinner.set_sub_state()``.
    Falls back to a static message in non-TTY environments.
    """
    from .pulse_spinner import PulseSpinner

    if not (hasattr(sys.stdout, "isatty") and sys.stdout.isatty()):
        sys.stdout.write(f"  {label}...\n")
        sys.stdout.flush()
        yield None
        return

    spinner = PulseSpinner(label)
    spinner.start()
    try:
        yield spinner
    finally:
        spinner.stop()


# ---------------------------------------------------------------------------
# Message rendering
# ---------------------------------------------------------------------------


def render_message(role: str, content: str) -> None:
    """Print a chat message with role prefix and markdown formatting."""
    if role == "assistant":
        if _use_color():
            _c(_Colors.BOLD + _Colors.ACCENT_BLUE, "neut>")
        print()
        for line in content.splitlines():
            formatted = format_markdown_line(line)
            print(f"  {formatted}")
        print()
    elif role == "user":
        pass  # User messages are already printed by the REPL
    elif role == "system":
        print(f"  {_c(_Colors.DIM, f'[system] {content}')}")
    elif role == "tool_result":
        print(f"  {_c(_Colors.DIM, content)}")


def render_welcome(gateway=None, show_banner: bool = False) -> None:
    """Print the chat welcome message with gateway status."""
    if show_banner:
        from axiom.setup.renderer import banner as mascot_banner

        mascot_banner()
    else:
        print()
        print(f"  {_c(_Colors.BOLD + _Colors.ACCENT_BLUE, 'neut chat')} — interactive agent")

    if gateway is not None:
        provider = gateway.active_provider
        if provider:
            status = _c(_Colors.GREEN, f"{provider.name} ({provider.model})")
        else:
            status = _c(_Colors.DIM, "stub mode (no LLM configured)")
        print(f"  Gateway: {status}")

    print(f"  Type {_c(_Colors.CYAN, '/help')} for commands, {_c(_Colors.CYAN, '/exit')} to quit.")
    print()


# ---------------------------------------------------------------------------
# Approval UI
# ---------------------------------------------------------------------------


def render_approval_prompt(action: Action) -> str:
    """Render an approval prompt and get user response.

    Returns:
        "a" for approve, "r" for reject
    """
    import shutil

    cols = shutil.get_terminal_size().columns
    divider_width = min(60, cols - 4)
    verb = getattr(action.category, "value", str(action.category)).lower()
    title = f"Approve {verb}"
    fill = max(0, divider_width - len(title) - 5)
    top = f"┌─ {title} {'─' * fill}┐"
    bot = f"└{'─' * (divider_width - 2)}┘"
    params_line = f"│ {_c(_Colors.BOLD, action.name)}: {_format_params(action.params, max_value=80)}"
    params_plain = f"│ {action.name}: {_format_params(action.params, max_value=80)}"

    print()
    if _use_color():
        print(f"  {_c(_Colors.DIM, top)}")
        print(f"  {params_line}")
        print(f"  {_c(_Colors.DIM, bot)}")
    else:
        print(f"  {top}")
        print(f"  {params_plain}")
        print(f"  {bot}")
    print(
        f"  [{_c(_Colors.GREEN, 'a')}]pprove  "
        f"[{_c(_Colors.GREEN, 'A')}]lways allow  "
        f"[{_c(_Colors.RED, 'r')}]eject  "
        f"[{_c(_Colors.RED, 'D')}]eny always  "
        f"[{_c(_Colors.DIM, 's')}]kip"
    )
    print()

    while True:
        try:
            raw = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            return "r"
        if raw == "A":
            return "A"
        if raw == "D":
            return "D"
        choice = raw.lower()
        if choice in ("a", "approve"):
            return "a"
        if choice in ("r", "reject", "s", "skip"):
            return "r"
        print("  Choose [a]pprove, [A]lways allow, [r]eject, [D]eny always, or [s]kip")


def render_action_result(action: Action) -> None:
    """Print the result of a completed action."""
    if action.status == ActionStatus.COMPLETED:
        result = action.result or {}
        if "error" in result:
            print(f"  {_c(_Colors.RED, '!')} {result['error']}")
        elif "url" in result:
            print(
                f"  {_c(_Colors.GREEN, 'Published:')} {result['url']} ({result.get('version', '')})"
            )
        elif "output" in result:
            print(f"  {_c(_Colors.GREEN, 'Generated:')} {result['output']}")
        elif "documents" in result:
            docs = result["documents"]
            if not docs:
                print("  No tracked documents.")
            else:
                for d in docs:
                    print(f"  {d['doc_id']}: {d['status']} ({d.get('version', '')})")
        elif "changed" in result:
            changed = result["changed"]
            if not changed:
                print("  No changes since last publish.")
            else:
                for c in changed:
                    print(f"  {c}")
        else:
            for k, v in result.items():
                print(f"  {k}: {v}")
    elif action.status == ActionStatus.REJECTED:
        print(f"  {_c(_Colors.DIM, '[skipped]')} {action.name}")
    elif action.status == ActionStatus.FAILED:
        print(f"  {_c(_Colors.RED, '[failed]')} {action.error}")


def render_session_list(sessions: list[dict[str, Any]]) -> None:
    """Render a formatted list of sessions."""
    from axiom.infra.text_utils import pluralize
    from axiom.infra.time_utils import time_ago

    if not sessions:
        print(f"  {_c(_Colors.DIM, 'No saved sessions.')}")
        return

    print()
    print(f"  {_c(_Colors.BOLD, 'Saved sessions:')}")
    for s in sessions:
        sid = s.get("id", "?")
        msgs = s.get("messages", 0)
        updated = s.get("updated", "")
        msg_text = pluralize(msgs, "message")
        when_str = time_ago(updated) if updated else ""
        print(
            f"  {_c(_Colors.CYAN, f'{sid:<10}')}  "
            f"{msg_text:<14}  "
            f"{when_str}"
        )
    print(f"  {_c(_Colors.DIM, 'Type a number, or /resume <id>.')}")
    print()


# ---------------------------------------------------------------------------
# File links (clickable in VS Code terminal)
# ---------------------------------------------------------------------------


def file_link(path: str | Path, line: int | None = None, display: str | None = None) -> str:
    """Format a file path as a clickable terminal link.

    In VS Code's integrated terminal, file paths are auto-detected and made
    clickable. This function ensures proper formatting and uses OSC 8
    hyperlinks for richer terminals.

    Args:
        path: Relative or absolute path to the file
        line: Optional line number to jump to
        display: Optional display text (defaults to path basename)

    Returns:
        Formatted string that's clickable in VS Code terminal
    """
    # Resolve to absolute path
    p = Path(path)
    if not p.is_absolute():
        p = _REPO_ROOT / p
    p = p.resolve()

    abs_path = str(p)
    if line:
        abs_path = f"{abs_path}:{line}"

    # Display text: use provided or basename
    if display is None:
        display = p.name
        if line:
            display = f"{display}:{line}"

    if not _use_color():
        return abs_path

    # Use OSC 8 hyperlink for terminals that support it
    # Format: \x1b]8;;URL\x07DISPLAY\x1b]8;;\x07
    uri = f"file://{p}"
    hyperlink = f"\x1b]8;;{uri}\x07{_c(_Colors.CYAN, display)}\x1b]8;;\x07"

    # Also include plain path for fallback (VS Code picks this up)
    return f"{hyperlink} ({_c(_Colors.DIM, abs_path)})"


def format_file_reference(path: str | Path, line: int | None = None) -> str:
    """Format a file reference for display in neut chat output.

    Creates a clickable link instead of showing file contents inline.
    """
    link = file_link(path, line)
    return f"\n  📄 {link}\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_params(params: dict[str, Any], max_value: int = 0) -> str:
    """Format action parameters for display.

    When max_value > 0, truncates each value string to max_value chars and
    appends a '[+N more chars]' marker.
    """
    if not params:
        return "(no parameters)"
    parts = []
    for k, v in params.items():
        v_str = str(v)
        if max_value > 0 and len(v_str) > max_value:
            extra = len(v_str) - 60
            v_str = v_str[:60] + f"… [+{extra} more chars]"
        parts.append(f"{k}={v_str}")
    return "  |  ".join(parts)
