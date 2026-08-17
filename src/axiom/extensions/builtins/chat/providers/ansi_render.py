# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ANSI render provider — zero-dependency fallback using existing renderer logic.

Wraps the current renderer.py functions into the RenderProvider interface.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from axiom.setup.renderer import _c, _Colors, _use_color

from .base import RenderProvider

if TYPE_CHECKING:
    from axiom.infra.gateway import StreamChunk
    from axiom.infra.orchestrator.actions import Action


# Markdown regexes (duplicated from renderer.py to keep this self-contained)
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_LIST_RE = re.compile(r"^(\s*)-\s+(.+)$")
_ORDERED_LIST_RE = re.compile(r"^(\s*)\d+\.\s+(.+)$")
_BLOCKQUOTE_RE = re.compile(r"^>\s?(.*)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _terminal_width() -> int:
    """Get terminal width, capped at 120 for readability."""
    try:
        import shutil

        cols = shutil.get_terminal_size().columns
        return min(cols, 120)
    except Exception:
        return 80


def _apply_inline(text: str) -> str:
    """Apply inline formatting: **bold**, `code`, [links](url)."""
    if not _use_color():
        return text

    # Web links → OSC 8
    def _linkify(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        return f"\x1b]8;;{url}\x07{_c(_Colors.CYAN, label)}\x1b]8;;\x07"

    text = _LINK_RE.sub(_linkify, text)
    text = _BOLD_RE.sub(lambda m: _c(_Colors.BOLD, m.group(1)), text)
    if "\x1b]8;;" not in text:
        text = _INLINE_CODE_RE.sub(lambda m: _c(_Colors.CYAN, f"`{m.group(1)}`"), text)
    return text


def _format_line(line: str, in_code_block: bool = False, code_lang: str = "") -> str:
    """Format a single markdown line with ANSI codes."""
    if not _use_color():
        return line

    if line.strip().startswith("```"):
        return _c(_Colors.DIM, line)

    m = _HEADING_RE.match(line)
    if m:
        return _c(_Colors.BOLD + _Colors.ACCENT_BLUE, line)

    m = _BLOCKQUOTE_RE.match(line)
    if m:
        return _c(_Colors.DIM, f"  > {m.group(1)}")

    m = _ORDERED_LIST_RE.match(line)
    if m:
        indent, content = m.group(1), m.group(2)
        content = _apply_inline(content)
        return f"{indent}{_c(_Colors.CYAN, '•')} {content}"

    m = _LIST_RE.match(line)
    if m:
        indent, content = m.group(1), m.group(2)
        content = _apply_inline(content)
        return f"{indent}{_c(_Colors.CYAN, '-')} {content}"

    return _apply_inline(line)


class AnsiRenderProvider(RenderProvider):
    """Zero-dependency render provider using ANSI escape codes."""

    def stream_text(self, chunks: Iterator[StreamChunk]) -> str:
        from ..pulse_spinner import PulseSpinner

        accumulated = ""
        in_code_block = False
        line_buffer = ""
        partial_displayed = 0
        is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

        # Show pulse spinner while waiting for first content
        spinner: PulseSpinner | None = None
        if is_tty:
            spinner = PulseSpinner("Thinking")
            spinner.start()

        def _stop_spinner():
            nonlocal spinner
            if spinner is not None:
                spinner.stop()
                spinner = None

        def _clear_partial():
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
            nonlocal partial_displayed
            if text:
                sys.stdout.write(text)
                sys.stdout.flush()
                partial_displayed = len(text)

        code_lang = ""  # language token from opening fence

        def _flush_line(line: str):
            nonlocal in_code_block, code_lang
            _clear_partial()

            if line.strip().startswith("```"):
                in_code_block = not in_code_block
                if in_code_block:
                    # Opening fence — parse language and emit framed header
                    code_lang = line.strip().lstrip("`").strip()
                    from axiom.infra.text_utils import code_block_lines as _cbl
                    header_lines = _cbl("", lang=code_lang)
                    sys.stdout.write(header_lines[0] + "\n")  # ┌─ lang
                    sys.stdout.flush()
                    return
                else:
                    # Closing fence — silent (no bottom rule clutter)
                    code_lang = ""
                    return
            elif in_code_block:
                if _use_color():
                    rule = _c(_Colors.DIM, "│ ")
                else:
                    rule = "│ "
                sys.stdout.write(f"{rule}{line}\n")
                sys.stdout.flush()
                return
            else:
                if _use_color():
                    line = _format_line(line)

            sys.stdout.write(line + "\n")
            sys.stdout.flush()

        for chunk in chunks:
            if chunk.type == "text":
                _stop_spinner()
                accumulated += chunk.text
                _clear_partial()
                line_buffer += chunk.text

                while "\n" in line_buffer:
                    line, line_buffer = line_buffer.split("\n", 1)
                    _flush_line(line)

                if line_buffer:
                    _show_partial(line_buffer)

            elif chunk.type == "thinking_delta":
                # Keep spinner alive during reasoning, update label
                if spinner is not None:
                    spinner.set_sub_state("reasoning")

            elif chunk.type == "usage":
                # Feed token counts into spinner while it's active
                if spinner is not None:
                    spinner.update_tokens(
                        input_tokens=chunk.input_tokens,
                        output_tokens=chunk.output_tokens,
                    )

            elif chunk.type == "tool_use_start":
                _stop_spinner()
                if line_buffer:
                    _clear_partial()
                    _flush_line(line_buffer)
                    line_buffer = ""

            elif chunk.type == "tool_use_end":
                pass

            elif chunk.type == "done":
                _stop_spinner()
                _clear_partial()
                if line_buffer:
                    if line_buffer.strip().startswith("```"):
                        in_code_block = not in_code_block
                    if _use_color() and in_code_block:
                        rule = _c(_Colors.DIM, "│ ")
                        line_buffer = f"{rule}{line_buffer}"
                    elif _use_color():
                        line_buffer = _format_line(line_buffer)
                    sys.stdout.write(line_buffer)
                    line_buffer = ""
                if accumulated and not accumulated.endswith("\n"):
                    sys.stdout.write("\n")
                sys.stdout.flush()
                break

        return accumulated

    def render_welcome(
        self,
        gateway=None,
        show_banner: bool = False,
        workspace_context: str = "",
    ) -> None:
        print()
        if show_banner:
            from axiom.setup.renderer import banner as mascot_banner

            mascot_banner()

        # Build header: axi chat · vX.Y.Z · model (provider) · cwd
        try:
            from importlib.metadata import version as _pkg_version

            _ver = _pkg_version("axiom-os-lm")
        except Exception:
            _ver = ""
        ver_seg = f" · v{_ver}" if _ver else ""

        model_seg = ""
        if gateway is not None:
            provider = gateway.active_provider
            if provider:
                model_name = getattr(gateway, "_model_override", None) or provider.model
                model_seg = f" · {model_name} ({provider.name})"
            else:
                model_seg = f"  {_c(_Colors.DIM, 'No LLM configured.')} " \
                             f"{_c(_Colors.DIM, 'Run `axi config` to set up.')}"

        import os as _os

        cwd_seg = f" · {_os.path.basename(_os.getcwd())}"

        from axiom.infra.text_utils import gutter

        if model_seg and not model_seg.startswith("  "):
            header_text = (
                f"{_c(_Colors.BOLD + _Colors.ACCENT_BLUE, 'axi chat')}"
                f"{_c(_Colors.DIM, ver_seg + model_seg + cwd_seg)}"
            )
            print(gutter(header_text))
        else:
            print(
                gutter(
                    f"{_c(_Colors.BOLD + _Colors.ACCENT_BLUE, 'axi chat')}"
                    f"{_c(_Colors.DIM, ver_seg + cwd_seg)}"
                )
            )
            if model_seg:
                print(model_seg)

        # Workspace context from model.yaml
        if workspace_context:
            from ..workspace import workspace_summary_line

            summary = workspace_summary_line(workspace_context)
            if summary:
                print(f"  {_c(_Colors.GREEN, summary)}")
            for line in workspace_context.splitlines()[1:]:
                if line.startswith("Materials:"):
                    print(f"    {_c(_Colors.DIM, line)}")
                    break

        # Example block
        _ex = 'Try: "summarize the README"  "@docs/CLAUDE.md what does this propose?"'
        print(f"  {_c(_Colors.DIM, _ex)}")
        # Footer
        print(
            f"  {_c(_Colors.DIM, '/help')}  "
            f"{_c(_Colors.DIM, '/model')}  "
            f"{_c(_Colors.DIM, '!cmd')}  "
            f"{_c(_Colors.DIM, '/exit')}"
        )
        print()

    def render_tool_start(self, name: str, params: dict[str, Any]) -> None:
        msg = f"  {_c(_Colors.DIM, f'[calling {name}...]')}"
        sys.stdout.write(f"\r{msg}")
        sys.stdout.flush()

    def render_tool_result(self, name: str, result: dict[str, Any], elapsed: float) -> None:
        if "error" in result:
            glyph = _c(_Colors.RED, "✗") if _use_color() else "FAIL"
            print(f"  {glyph} {name} failed ({elapsed:.1f}s): {result['error']}")
        else:
            glyph = _c(_Colors.GREEN, "✓") if _use_color() else "OK"
            print(f"  {glyph} {name} ({elapsed:.1f}s)")

    def render_approval_prompt(self, action: Action) -> str:
        verb = getattr(action.category, "value", str(action.category)).lower()
        title = f"Approve {verb}"
        fill = max(0, 30 - len(title) - 5)
        top = f"┌─ {title} {'─' * fill}┐"
        bot = f"└{'─' * 28}┘"
        print()
        if _use_color():
            print(f"  {_c(_Colors.DIM, top)}")
            print(f"  │ {_c(_Colors.BOLD, action.name)}: {_format_params(action.params)}")
            print(f"  {_c(_Colors.DIM, bot)}")
        else:
            print(f"  {top}")
            print(f"  │ {action.name}: {_format_params(action.params)}")
            print(f"  {bot}")
        print(
            f"  [{_c(_Colors.GREEN, 'a')}]pprove  "
            f"[{_c(_Colors.GREEN, 'A')}]lways allow  "
            f"[{_c(_Colors.RED, 'r')}]eject  "
            f"[{_c(_Colors.DIM, 's')}]kip"
        )
        print()

        while True:
            try:
                choice = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                return "r"
            if choice in ("a", "approve"):
                return "a"
            elif choice == "A":
                return "A"
            elif choice in ("r", "reject", "s", "skip"):
                return "r"
            else:
                print("  Choose [a]pprove, [A]lways allow, [r]eject, or [s]kip")

    def render_action_result(self, action: Action) -> None:
        from axiom.infra.orchestrator.actions import ActionStatus

        if action.status == ActionStatus.COMPLETED:
            result = action.result or {}
            if "error" in result:
                print(f"  {_c(_Colors.RED, '!')} {result['error']}")
            elif "url" in result:
                print(
                    f"  {_c(_Colors.GREEN, 'Published:')} {result['url']} "
                    f"({result.get('version', '')})"
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

    def render_status(
        self,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost: float,
        tier: str | None = None,
    ) -> None:
        parts = []
        if model:
            parts.append(model)
        parts.append(f"{tokens_in:,} in")
        parts.append(f"{tokens_out:,} out")
        if cost > 0:
            parts.append(f"${cost:.4f}")
        if tier:
            parts.append(f"[{tier}]")
        line = " · ".join(parts)
        print(f"  {_c(_Colors.DIM, line)}")

    def render_thinking(self, text: str, collapsed: bool = True) -> None:
        if not text:
            return
        from axiom.infra.text_utils import pluralize

        lines = text.splitlines()
        if collapsed and len(lines) > 3:
            hidden = len(lines) - 3
            display = lines[:3]
            display.append(
                f"… ({pluralize(hidden, 'more line')}, Alt+T to expand)"
            )
        else:
            display = lines

        # Header once, then body lines with left-rail prefix
        header = _c(_Colors.DIM, "[thinking]") if _use_color() else "[thinking]"
        print(f"  {header}")
        rule = _c(_Colors.DIM, "│ ") if _use_color() else "│ "
        corner = _c(_Colors.DIM, "└─") if _use_color() else "└─"
        for line in display:
            formatted = _c(_Colors.DIM, line) if _use_color() else line
            print(f"  {rule}{formatted}")
        print(f"  {corner}")

    def render_message(self, role: str, content: str) -> None:
        if role == "assistant":
            print()
            for line in content.splitlines():
                formatted = _format_line(line)
                print(f"  {formatted}")
            print()
        elif role == "user":
            pass
        elif role == "system":
            print(f"  {_c(_Colors.DIM, f'[system] {content}')}")
        elif role == "tool_result":
            print(f"  {_c(_Colors.DIM, content)}")

    def render_session_list(self, sessions: list[dict[str, Any]]) -> None:
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


def _format_params(params: dict[str, Any]) -> str:
    if not params:
        return "(no parameters)"
    parts = []
    for k, v in params.items():
        parts.append(f"{k}={v}")
    return "  |  ".join(parts)
