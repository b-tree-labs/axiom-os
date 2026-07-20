# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Full-screen chat TUI — fixed bottom input with scrollable output.

Replaces the sequential print-based REPL with a prompt_toolkit Application
using a split layout:

    ┌─────────────────────────────────────┐
    │  Output (scrollable, styled)        │  Buffer + BufferControl + Lexer
    │  you> Hey there.                    │
    │  Hello! I'm neut...                 │
    │  · Thinking… (3s · esc to cancel)   │  ConditionalContainer
    │     claude-sonnet · 245in/1840out   │  FormattedTextControl (status, right)
    │  ─────────────────────────────────  │  FormattedTextControl (border)
    │  you> [cursor here]                 │  Buffer + BufferControl + BeforeInput
    │  ─────────────────────────────────  │  FormattedTextControl (border)
    │  >> ask mode  (shift+tab)  · ctrl+d │  FormattedTextControl (toolbar)
    └─────────────────────────────────────┘

Threading model:
    Main thread  — prompt_toolkit event loop (app.run())
    Agent thread — agent.turn() in daemon Thread
    Spinner      — animates FormattedText at 60ms, calls app.invalidate()
    Approval     — agent thread blocks on Event; main thread unblocks
"""

from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.clipboard import ClipboardData
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import ConditionalKeyBindings, KeyBindings, merge_key_bindings
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    Float,
    FloatContainer,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.margins import Margin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import BeforeInput, Processor, Transformation
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.selection import SelectionState, SelectionType
from prompt_toolkit.styles import Style, merge_styles

from axiom.extensions.builtins.hygiene import acquire_dir
from axiom.infra.hash_utils import MEDIUM, fingerprint
from axiom.infra.time_utils import time_ago

from .providers.base import RenderProvider
from .pulse_spinner import (
    _FRAME_INTERVAL,
    PULSE_FRAMES,
    _format_elapsed,
    _format_tokens,
)

if TYPE_CHECKING:
    from axiom.infra.gateway import StreamChunk
    from axiom.infra.orchestrator.actions import Action
    from axiom.infra.orchestrator.session import SessionStore

    from .agent import ChatAgent

from axiom.extensions.builtins.update.background import BackgroundUpdateChecker
from axiom.extensions.builtins.update.version_check import VersionInfo

# ---------------------------------------------------------------------------
# Color theme — matches Cherenkov blue brand from setup/renderer.py
# ---------------------------------------------------------------------------

_CHERENKOV = "#00cfff"

_CHERENKOV_DIM = "#009fcc"  # Slightly muted Cherenkov for secondary elements

_STYLE = Style.from_dict(
    {
        # Markdown in output
        "md.heading": f"{_CHERENKOV} bold",
        "md.bold": "bold",
        "md.code": _CHERENKOV_DIM,
        "md.list-bullet": _CHERENKOV_DIM,
        "md.blockquote": "#6c6c6c italic",
        # Semantic lines
        "dim": "#6c6c6c",
        "user-prefix": f"{_CHERENKOV} bold",
        "success": "#5faf5f",
        "error": "#d75f5f",
        "warning": "#d7af5f",
        "welcome": f"{_CHERENKOV} bold",
        "gateway.ok": "#5faf5f",
        "gateway.stub": "#d7af5f",
        "slash-cmd": _CHERENKOV_DIM,
        # UI chrome
        "border": "#6c6c6c",
        "prompt": f"{_CHERENKOV} bold",
        "toolbar.arrow": _CHERENKOV,
        "toolbar.mode": f"{_CHERENKOV} bold",
        "toolbar.dim": "#6c6c6c",
        "toolbar.approval": "#d7af5f",
        "status": "#6c6c6c",
        "spinner.detail": "#6c6c6c",
        "placeholder": "#585858 italic",
        # Tables
        "table.pipe": _CHERENKOV_DIM,
        "table.header": f"{_CHERENKOV_DIM} bold",
        "table.separator": "#6c6c6c",
        # Mermaid / diagrams
        "diagram.label": f"{_CHERENKOV} bold",
        "diagram.code": "#7a9cc7",
        "diagram.keyword": "#7a9cc7 bold",
        "diagram.rendered": "#5faf5f italic",
        # Autocomplete menu
        "completion-menu": "bg:#1a1a2e #c0c0c0",
        "completion-menu.completion": "bg:#1a1a2e #c0c0c0",
        "completion-menu.completion.current": f"bg:{_CHERENKOV_DIM} #000000",
        # Text selection (prompt_toolkit uses "class:selected" for selection highlight)
        "selected": "bg:#264f78",
        # Scroll indicator
        "scrollbar.thumb": "bg:#444444",
        # Session picker
        "picker.header": f"{_CHERENKOV} bold",
        "picker.cursor": f"{_CHERENKOV} bold",
        "picker.check": "#5faf5f bold",
        "picker.uncheck": "#6c6c6c",
        "picker.sid": _CHERENKOV_DIM,
        "picker.meta": "#6c6c6c",
        "picker.detail": "#6c6c6c italic",
    }
)

# Sci Displays (ADR-039 D12) — Pygments-token styles for in-line code-block
# rendering. The Axiom dark theme is the source of truth; we merge it into
# the prompt_toolkit style sheet so the lexer can emit
# ``class:pygments.keyword`` etc. and have them styled with the brand palette.
try:
    from prompt_toolkit.styles.pygments import style_from_pygments_cls

    from axiom.extensions.builtins.scidisplay.themes import AxiomDarkTheme

    _STYLE = merge_styles([_STYLE, style_from_pygments_cls(AxiomDarkTheme)])
except Exception:
    # scidisplay not installed (unusual install) — fall back to dim code blocks.
    pass


# ---------------------------------------------------------------------------
# Output lexer — styles each line of the output buffer
# ---------------------------------------------------------------------------

_RE_HEADING = re.compile(r"^(\s*)(#{1,3})\s+(.+)$")
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_RE_CODE = re.compile(r"`([^`]+)`")
_RE_LIST = re.compile(r"^(\s*)[-*]\s+(.+)$")
_RE_ORDERED = re.compile(r"^(\s*)\d+\.\s+(.+)$")
_RE_STATUS = re.compile(r"^\s+\S.*\d+in/\d+out")
_RE_SLASH = re.compile(r"^(\s+)(/\w+)(\s+.*)$")
_RE_TABLE_ROW = re.compile(r"^(\s*)[|\u2502](.+)[|\u2502]\s*$")
_RE_TABLE_SEP = re.compile(r"^(\s*)[|\u2502][\s:]*-[-\s:|]*[|\u2502]\s*$")

# Sci Displays code-rendering bridge — Pygments tokenization → prompt_toolkit
# class names. The merged style sheet (above) maps these classes to brand
# colors, so the lexer just emits the right class strings here.
#
# Per-language lexer cache: get_lexer_by_name() is non-trivial cost, and we
# call it once per visible line during rendering.
_PYGMENTS_LEXER_CACHE: dict[str, Any] = {}


def _resolve_pygments_lexer(language: str):
    """Return a cached Pygments lexer for ``language``, or None on miss."""
    if language in _PYGMENTS_LEXER_CACHE:
        return _PYGMENTS_LEXER_CACHE[language]
    try:
        from pygments.lexers import get_lexer_by_name

        lexer = get_lexer_by_name(language, stripnl=False, ensurenl=False)
    except Exception:
        lexer = None
    _PYGMENTS_LEXER_CACHE[language] = lexer
    return lexer


def _pygments_token_to_class(token) -> str:
    """Map a Pygments Token to a prompt_toolkit class name.

    Format: ``class:pygments.keyword.constant`` for ``Token.Keyword.Constant``.
    The ``Token`` prefix is dropped (per prompt_toolkit's
    ``style_from_pygments_cls`` convention) and replaced with ``pygments``;
    rest is lowercased dotted path. The class root is ``pygments`` so the
    style sheet from ``style_from_pygments_cls`` matches.
    """
    parts = [p.lower() for p in str(token).split(".")]
    if parts and parts[0] == "token":
        parts = parts[1:]
    if not parts:
        return "class:pygments"
    return "class:pygments." + ".".join(parts)


def _style_code_line(line: str, language: str) -> list[tuple[str, str]]:
    """Tokenize one line via Pygments and return prompt_toolkit-styled tuples.

    Falls back to a single ``class:dim`` tuple if the language can't be
    resolved or tokenization raises — never breaks the lexer pipeline.
    """
    lexer = _resolve_pygments_lexer(language)
    if lexer is None:
        return [("class:dim", line)]
    try:
        tokens = list(lexer.get_tokens(line))
    except Exception:
        return [("class:dim", line)]
    return [
        (_pygments_token_to_class(tok), text)
        for tok, text in tokens
        if text  # skip empty fragments
    ]


# Mermaid keywords for syntax highlighting
_MERMAID_KEYWORDS = {
    "graph",
    "subgraph",
    "end",
    "flowchart",
    "sequenceDiagram",
    "classDiagram",
    "stateDiagram",
    "erDiagram",
    "gantt",
    "pie",
    "gitgraph",
    "mindmap",
    "timeline",
    "style",
    "class",
    "click",
    "participant",
    "actor",
    "note",
    "loop",
    "alt",
    "opt",
    "par",
    "LR",
    "RL",
    "TB",
    "BT",
    "TD",
}


class _OutputLexer(Lexer):
    """Applies markdown-like styling to the output buffer line-by-line.

    Also overlays ``class:selected`` on the active mouse-drag selection
    (read from ``_ScrollableBufferControl.sel_start / sel_end``).  This
    is done in the lexer instead of ``HighlightSelectionProcessor`` because
    focus stays on the input buffer during drag — the built-in processor
    only highlights the *focused* control.
    """

    def __init__(self, control: _ScrollableBufferControl | None = None):
        self._control = control

    def invalidation_hash(self):
        # Include selection range so the fragment cache is busted when the
        # user drags a new selection (text doesn't change, but styling does).
        ctrl = self._control
        if ctrl:
            return (id(self), ctrl.sel_start, ctrl.sel_end)
        return id(self)

    def lex_document(self, document):  # type: ignore[override]
        lines = document.lines

        # Snapshot the selection range (may change between renders)
        ctrl = self._control
        sel_start = ctrl.sel_start if ctrl else None
        sel_end = ctrl.sel_end if ctrl else None

        # Pre-process: track code-fence state and language across lines
        styled: list[list[tuple[str, str]]] = []
        in_code = False
        code_lang = ""

        for i, line in enumerate(lines):
            stripped = line.strip()

            # --- Code fences ---
            if stripped.startswith("```"):
                if not in_code:
                    in_code = True
                    code_lang = stripped[3:].strip().lower()
                    if code_lang == "mermaid":
                        styled.append(
                            [
                                ("class:dim", line[: line.find("```")]),
                                ("class:diagram.label", "\u25b8 Mermaid Diagram"),
                            ]
                        )
                        continue
                    styled.append([("class:dim", line)])
                    continue
                in_code = False
                if code_lang == "mermaid":
                    styled.append(
                        [
                            ("class:dim", line[: line.find("```")]),
                            ("class:diagram.label", "\u25c2 end diagram"),
                        ]
                    )
                else:
                    styled.append([("class:dim", line)])
                code_lang = ""
                continue

            if in_code:
                if code_lang == "mermaid":
                    styled.append(self._style_mermaid_line(line))
                elif code_lang:
                    # Sci Displays code rendering: Pygments-tokenize the line
                    # and emit class:pygments.<token> tuples so the merged
                    # style sheet colors it with the Axiom brand palette.
                    styled.append(_style_code_line(line, code_lang))
                else:
                    styled.append([("class:dim", line)])
                continue

            # --- Table rows ---
            if _RE_TABLE_SEP.match(line):
                styled.append([("class:table.separator", line)])
                continue

            m = _RE_TABLE_ROW.match(line)
            if m:
                # Check if next line is a separator → this is a header row
                is_header = False
                if i + 1 < len(lines) and _RE_TABLE_SEP.match(lines[i + 1]):
                    is_header = True
                styled.append(self._style_table_row(line, is_header))
                continue

            styled.append(self._style_line(line))

        # --- Overlay mouse-drag selection highlight ---
        if sel_start is not None and sel_end is not None:
            _apply_selection_overlay(styled, lines, sel_start, sel_end)

        def get_line(lineno: int) -> list[tuple[str, str]]:
            if lineno < len(styled):
                return styled[lineno]
            return [("", "")]

        return get_line

    # -- line-level dispatch ------------------------------------------------

    def _style_line(self, line: str) -> list[tuple[str, str]]:
        stripped = line.strip()

        # Rendered mermaid diagram placeholder
        if stripped.startswith("\u25b8 Diagram rendered"):
            return [("class:diagram.rendered", line)]

        # Picker lines
        if stripped.startswith("\u2514 "):
            return [("class:picker.detail", line)]
        if stripped.startswith(("Select a session", "Archive sessions")):
            return [("class:picker.header", line)]
        if line.startswith(" > "):
            return [("class:picker.cursor", line)]
        if stripped.startswith(("[x]", "[ ]")):
            # Multi-select row (not highlighted)
            idx = line.index("[")
            check = line[idx : idx + 3]
            rest = line[idx + 3 :]
            style = "class:picker.check" if check == "[x]" else "class:picker.uncheck"
            return [
                ("", line[:idx]),
                (style, check),
                ("class:picker.meta", rest),
            ]
        # Single-select non-cursor rows: "   <12-char-hex>  ..."
        if (
            len(stripped) > 12
            and re.match(r"^[0-9a-f]{12}\b", stripped)
            and not stripped.startswith("you> ")
        ):
            idx = line.index(stripped[:12])
            return [
                ("", line[:idx]),
                ("class:picker.sid", stripped[:12]),
                ("class:picker.meta", line[idx + 12 :]),
            ]

        # User message echo: "you> ..."
        if stripped.startswith("you> "):
            idx = line.index("you> ")
            return [
                ("", line[:idx]),
                ("class:user-prefix", "you> "),
                ("class:md.bold", line[idx + 5 :]),
            ]

        # Headings: ## ... — strip ** markers and leading emoji
        m = _RE_HEADING.match(line)
        if m:
            indent = m.group(1)  # leading whitespace
            text = m.group(3).replace("**", "")  # heading text
            # Strip leading emoji (Unicode emoji + variation selectors)
            text = re.sub(r"^[\U0001f300-\U0001f9ff\u2600-\u27bf\ufe0f\u200d]+\s*", "", text)
            return [("class:md.heading", f"{indent}{text}")]

        # Status line: model | 1234in/567out
        if _RE_STATUS.match(line):
            return [("class:dim", line)]

        # Tool results
        if stripped.startswith("v ") and "(" in stripped:
            return [("class:success", line)]
        if stripped.startswith("x ") and ("failed" in stripped or "(" in stripped):
            return [("class:error", line)]

        # System / thinking / status
        if stripped.startswith("[system]") or stripped.startswith("[thinking]"):
            return [("class:dim", line)]
        if stripped.startswith("[skipped]"):
            return [("class:warning", line)]
        if stripped.startswith("[failed]") or stripped.startswith("[error]"):
            return [("class:error", line)]

        # Approval prompt
        if stripped.startswith("--- Write operation") or stripped == "-" * 30:
            return [("class:warning", line)]

        # Mascot / banner (box-drawing characters)
        if any(ch in stripped for ch in "\u256d\u256e\u2570\u256f\u25d5\u2550\u2518\u2514"):
            return [("class:welcome", line)]

        # Welcome version line: "  Neut v0.1.0 — ..."
        if stripped.startswith("Neut v") and "\u2014" in stripped:
            return [("class:welcome", line)]

        # Metadata lines: org line, path line
        if stripped.startswith("  ") and (
            stripped.endswith("Engineering") or stripped.startswith("/")
        ):
            return [("class:dim", line)]

        # Help hint line
        if stripped.startswith("Type /help"):
            return self._style_help_hint(line)

        # Slash command in help listing: "  /help   Show this help"
        m = _RE_SLASH.match(line)
        if m:
            return [
                ("", m.group(1)),
                ("class:slash-cmd", m.group(2)),
                ("", m.group(3)),
            ]

        # Bold section headers (e.g., "  Chat Commands:")
        if stripped.endswith(":") and not stripped.startswith("-"):
            if _RE_BOLD.search(stripped) is None and len(stripped) < 60:
                return [("class:md.bold", line)]

        # List items: - ...
        m = _RE_LIST.match(line)
        if m:
            return self._style_list_item(m.group(1), "-", m.group(2))

        m = _RE_ORDERED.match(line)
        if m:
            indent = m.group(1)
            rest = line[len(indent) :]
            dot_pos = rest.index(". ")
            num = rest[: dot_pos + 2]
            content = rest[dot_pos + 2 :]
            return [
                ("", indent),
                ("class:md.list-bullet", num),
                *self._parse_inline(content),
            ]

        # Blockquote: > ...
        if stripped.startswith(">"):
            return [("class:md.blockquote", line)]

        # Default: inline markdown
        return self._parse_inline(line)

    # -- helpers ------------------------------------------------------------

    def _style_list_item(
        self,
        indent: str,
        bullet: str,
        content: str,
    ) -> list[tuple[str, str]]:
        return [
            ("", indent),
            ("class:md.list-bullet", f"{bullet} "),
            *self._parse_inline(content),
        ]

    def _parse_inline(self, line: str) -> list[tuple[str, str]]:
        """Parse **bold** and `code` inline markers."""
        if not line:
            return [("", "")]

        # Collect matches
        matches: list[tuple[int, int, str, str]] = []
        for m in _RE_BOLD.finditer(line):
            matches.append((m.start(), m.end(), "class:md.bold", m.group(1)))
        for m in _RE_CODE.finditer(line):
            matches.append(
                (m.start(), m.end(), "class:md.code", f"`{m.group(1)}`"),
            )

        # Sort by position, drop overlaps
        matches.sort(key=lambda x: x[0])
        filtered: list[tuple[int, int, str, str]] = []
        last_end = 0
        for start, end, style, text in matches:
            if start >= last_end:
                filtered.append((start, end, style, text))
                last_end = end

        # Build fragments
        fragments: list[tuple[str, str]] = []
        pos = 0
        for start, end, style, text in filtered:
            if start > pos:
                fragments.append(("", line[pos:start]))
            fragments.append((style, text))
            pos = end

        if pos < len(line):
            fragments.append(("", line[pos:]))

        return fragments if fragments else [("", line)]

    def _style_mermaid_line(self, line: str) -> list[tuple[str, str]]:
        """Style a line inside a ```mermaid block with keyword highlighting."""
        stripped = line.strip()
        if not stripped:
            return [("class:diagram.code", line)]

        # Highlight keywords at the start of lines
        first_word = stripped.split()[0].rstrip(":")
        if first_word in _MERMAID_KEYWORDS:
            idx = line.index(first_word)
            return [
                ("class:diagram.code", line[:idx]),
                ("class:diagram.keyword", first_word),
                ("class:diagram.code", line[idx + len(first_word) :]),
            ]

        # Style directives (e.g., "style A fill:#ff5722,color:#fff")
        if stripped.startswith("style ") or stripped.startswith("class "):
            return [("class:dim", line)]

        return [("class:diagram.code", line)]

    def _style_table_row(
        self,
        line: str,
        is_header: bool,
    ) -> list[tuple[str, str]]:
        """Style a markdown table row with colored pipes and header bold."""
        # Normalise box-drawing │ to ASCII | before splitting
        normalised = line.replace("\u2502", "|")
        fragments: list[tuple[str, str]] = []
        parts = normalised.split("|")

        for i, part in enumerate(parts):
            if i > 0:
                fragments.append(("class:table.pipe", "\u2502"))  # │ instead of |
            if part:
                if is_header:
                    fragments.append(("class:table.header", part))
                else:
                    fragments.extend(self._parse_inline(part))

        return fragments if fragments else [("", line)]

    def _style_help_hint(self, line: str) -> list[tuple[str, str]]:
        parts: list[tuple[str, str]] = []
        pos = 0
        for cmd in ("/help", "/exit"):
            idx = line.find(cmd, pos)
            if idx >= 0:
                if idx > pos:
                    parts.append(("class:dim", line[pos:idx]))
                parts.append(("class:slash-cmd", cmd))
                pos = idx + len(cmd)
        if pos < len(line):
            parts.append(("class:dim", line[pos:]))
        return parts if parts else [("class:dim", line)]


# ---------------------------------------------------------------------------
# Table alignment — reformats markdown tables with padded columns
# ---------------------------------------------------------------------------


def _align_table(lines: list[str], max_width: int) -> list[str]:
    """Reformat a markdown table block with aligned, padded columns."""
    if not lines:
        return lines

    # Parse each row into cells (normalise box-drawing │ to |)
    rows: list[tuple[str, list[str], bool]] = []  # (indent, cells, is_sep)
    for line in lines:
        normalised = line.replace("\u2502", "|")
        stripped = normalised.lstrip()
        indent = normalised[: len(normalised) - len(stripped)]
        is_sep = bool(_RE_TABLE_SEP.match(normalised))
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        rows.append((indent, cells, is_sep))

    if not rows:
        return lines

    # Calculate column widths (ignore separators for width calc)
    num_cols = max(len(cells) for _, cells, _ in rows)
    widths = [0] * num_cols
    for indent, cells, is_sep in rows:
        if is_sep:
            continue
        for j, cell in enumerate(cells):
            if j < num_cols:
                widths[j] = max(widths[j], len(cell))

    # Ensure minimum width of 3 per column
    widths = [max(w, 3) for w in widths]

    # Reformat rows
    result: list[str] = []
    for indent, cells, is_sep in rows:
        padded: list[str] = []
        for j in range(num_cols):
            cell = cells[j] if j < len(cells) else ""
            if is_sep:
                padded.append("-" * widths[j])
            else:
                padded.append(cell.ljust(widths[j]))
        result.append(f"{indent}| {' | '.join(padded)} |")

    return result


# ---------------------------------------------------------------------------
# ANSI stripper for slash command output
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[^m]*m|\x1b\]8;;[^\x07]*\x07")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# Mermaid diagram rendering — background SVG generation via mmdc
# ---------------------------------------------------------------------------

_MERMAID_BLOCK_RE = re.compile(
    r"```mermaid\s*\n(.*?)```",
    re.DOTALL,
)

_MMDC_PATH: str | None = shutil.which("mmdc")


def _render_mermaid_svg(code: str, diagram_dir: Path) -> str | None:
    """Render mermaid code to SVG via mmdc.

    Args:
        code: Mermaid diagram source.
        diagram_dir: Writable directory for input/output files.

    Returns:
        SVG file path, or None on failure (missing mmdc, permission error,
        render failure, timeout).
    """
    if not _MMDC_PATH:
        return None

    digest = fingerprint(code, length=MEDIUM)
    input_path = diagram_dir / f"input-{digest}.mmd"
    output_path = diagram_dir / f"diagram-{digest}.svg"

    # Skip re-render if SVG already exists for this exact code
    if output_path.exists():
        return str(output_path)

    try:
        input_path.write_text(code, encoding="utf-8")
        subprocess.run(
            [
                _MMDC_PATH,
                "-i",
                str(input_path),
                "-o",
                str(output_path),
                "-t",
                "dark",
                "-b",
                "transparent",
            ],
            capture_output=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError, PermissionError):
        return None

    if output_path.exists():
        return str(output_path)
    return None


def _open_file(path: str) -> None:
    """Open a file with the system viewer (non-blocking)."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "linux":
            subprocess.Popen(
                ["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
    except OSError:
        pass


def _make_mermaid_placeholder(svg_path: str) -> str:
    """Build a single-line placeholder for a rendered mermaid diagram."""
    return f"  \u25b8 Diagram rendered \u2192 {svg_path}"


# ---------------------------------------------------------------------------
# Math block processing \u2014 mirrors Mermaid; per Sci Displays spec \u00a76.4
# ---------------------------------------------------------------------------


_RE_MATH_BLOCK = re.compile(r"```math\n(.*?)```", re.DOTALL)


def _make_math_placeholder(receipt_id: str, image_path: str | None) -> str:
    """Single-line placeholder for a rendered equation."""
    if image_path:
        return f"  \u25b8 Equation rendered \u2192 {image_path}  ({receipt_id})"
    return f"  \u25b8 Equation receipt \u2192 {receipt_id}"


def _process_math_blocks(raw: str, rendered: dict[str, str | None]) -> str:
    """Find ```math fences in ``raw``, render each, replace with placeholders.

    ``rendered`` is a content-keyed cache (mutated in place) so re-runs
    over an unchanged buffer are free. Renderer never raises \u2014 failed
    blocks fall through with the source visible (better than a silent drop).
    """
    try:
        from axiom.extensions.builtins.scidisplay.math_render import (
            MathBlock,
            render_math,
        )
    except Exception:
        return raw

    def _replace(m: re.Match) -> str:
        latex = m.group(1).strip()
        if not latex:
            return m.group(0)
        if latex in rendered:
            placeholder = rendered[latex]
            if placeholder:
                return placeholder
            return m.group(0)
        try:
            result = render_math(MathBlock(latex=latex))
        except Exception:
            rendered[latex] = None
            return m.group(0)
        image_path = (
            str(result.svg_path) if result.svg_path
            else (str(result.png_path) if result.png_path else None)
        )
        placeholder = _make_math_placeholder(result.receipt_id, image_path)
        rendered[latex] = placeholder
        return placeholder

    return _RE_MATH_BLOCK.sub(_replace, raw)


def _process_mermaid_blocks(
    raw: str,
    rendered: dict[str, str | None],
    diagram_dir: Path,
) -> str:
    """Find complete mermaid blocks in raw text, render + replace with placeholders.

    Args:
        raw: The full raw output text.
        rendered: Cache mapping mermaid code -> svg_path (mutated in place).
        diagram_dir: Writable directory for rendered files.

    Returns:
        Updated raw text with rendered blocks replaced by placeholders.
    """
    if not _MMDC_PATH:
        return raw

    def _replace(m: re.Match) -> str:
        code = m.group(1).strip()
        if not code:
            return m.group(0)

        # Check cache first
        if code in rendered:
            svg_path = rendered[code]
            if svg_path:
                return _make_mermaid_placeholder(svg_path)
            # None means render failed — keep raw
            return m.group(0)

        # Render in-line (called from background thread already)
        svg_path = _render_mermaid_svg(code, diagram_dir)
        rendered[code] = svg_path  # cache even failures (as None)
        if svg_path:
            _open_file(svg_path)
            return _make_mermaid_placeholder(svg_path)
        return m.group(0)

    return _MERMAID_BLOCK_RE.sub(_replace, raw)


# ---------------------------------------------------------------------------
# Selection overlay — applies class:selected to lexer fragments
# ---------------------------------------------------------------------------


def _apply_selection_overlay(
    styled: list[list[tuple[str, str]]],
    lines: list[str],
    sel_start: int,
    sel_end: int,
) -> None:
    """Overlay ``class:selected`` on *styled* fragments for the range
    [sel_start, sel_end) in the buffer text.

    Operates in-place on *styled* (one entry per document line).
    *lines* are the raw text lines from the document (``document.lines``).
    """
    if sel_start >= sel_end:
        return

    char_pos = 0  # running cursor through the full buffer text
    for lineno, line in enumerate(lines):
        line_len = len(line)
        line_end = char_pos + line_len  # exclusive (does not include the \n)

        # Does this line overlap the selection?
        if char_pos < sel_end and line_end > sel_start:
            from_col = max(0, sel_start - char_pos)
            to_col = min(line_len, sel_end - char_pos)
            if from_col < to_col and lineno < len(styled):
                styled[lineno] = _highlight_fragments(
                    styled[lineno],
                    from_col,
                    to_col,
                )

        char_pos = line_end + 1  # +1 for the \n
        if char_pos >= sel_end:
            break


def _highlight_fragments(
    fragments: list[tuple[str, str]],
    from_col: int,
    to_col: int,
) -> list[tuple[str, str]]:
    """Return a new fragment list with ``class:selected`` applied from
    *from_col* to *to_col* (character offsets within the line)."""
    result: list[tuple[str, str]] = []
    col = 0
    for style, text in fragments:
        frag_end = col + len(text)
        if frag_end <= from_col or col >= to_col:
            # Entirely outside selection
            result.append((style, text))
        elif col >= from_col and frag_end <= to_col:
            # Entirely inside selection
            result.append((style + " class:selected", text))
        else:
            # Partial overlap — split the fragment
            before = text[: max(0, from_col - col)]
            middle = text[max(0, from_col - col) : min(len(text), to_col - col)]
            after = text[min(len(text), to_col - col) :]
            if before:
                result.append((style, before))
            if middle:
                result.append((style + " class:selected", middle))
            if after:
                result.append((style, after))
        col = frag_end
    return result


# ---------------------------------------------------------------------------
# Fast-scroll BufferControl — higher mouse wheel sensitivity
# ---------------------------------------------------------------------------


class _ScrollMetricsCapture(Margin):
    """Zero-width invisible margin that captures scroll metrics from window_render_info.

    The captured metrics (content_height, window_height, vertical_scroll) are
    stored on the TUI instance and read by the full-height scrollbar column.
    """

    def __init__(self, tui):
        self._tui = tui

    def get_width(self, get_ui_content):  # noqa: ARG002
        return 0

    def create_margin(self, window_render_info, width, height):  # noqa: ARG002
        if window_render_info is not None:
            self._tui._scroll_content_height = window_render_info.content_height
            self._tui._scroll_window_height = window_render_info.window_height
            self._tui._scroll_vertical_scroll = window_render_info.vertical_scroll
            self._tui._scroll_window_width = window_render_info.window_width
        return []


class _ScrollableBufferControl(BufferControl):
    """BufferControl with fast mouse scroll and click-drag text selection.

    Focus never leaves the input buffer — the cursor stays blinking in the
    input area at all times.  Selection is tracked internally via
    ``_sel_start`` / ``_sel_end`` (buffer cursor positions) and rendered by
    the ``_OutputLexer`` which reads these values.  On mouse-up, selected
    text is auto-copied to the system clipboard (terminal-style behaviour).
    """

    def __init__(self, tui: FullScreenChat, **kwargs):
        super().__init__(**kwargs)
        self._tui = tui
        self._drag_start: int | None = None  # selection anchor (persists for shift ops)
        self._dragging: bool = False  # True only while mouse button is held
        # Selection range (inclusive start, exclusive end) — read by _OutputLexer
        self.sel_start: int | None = None
        self.sel_end: int | None = None
        # Scroll velocity tracking
        self._last_scroll_time: float = 0.0

    # -- Position translation ------------------------------------------------

    def _mouse_pos_to_cursor(self, row: int, col: int) -> int:
        """Translate mouse (line_number, column) to a buffer cursor position.

        prompt_toolkit's ``Window`` mouse handler already translates screen
        coordinates into content coordinates — ``row`` is the **buffer line
        number** and ``col`` is the **character column** within that line.
        No scroll offset or wrapping math is needed here.
        """
        return self.buffer.document.translate_row_col_to_index(row, col)

    # -- Mouse handler -------------------------------------------------------

    def mouse_handler(self, mouse_event):
        from prompt_toolkit.mouse_events import MouseEventType

        try:
            from prompt_toolkit.mouse_events import MouseModifier
        except ImportError:
            MouseModifier = None

        def _has_shift() -> bool:
            if MouseModifier is None:
                return False
            mods = getattr(mouse_event, "modifiers", None)
            if mods is None:
                return False
            return MouseModifier.SHIFT in mods

        # --- Scroll ---
        if mouse_event.event_type in (
            MouseEventType.SCROLL_UP,
            MouseEventType.SCROLL_DOWN,
        ):
            window = self._tui._output_window

            # Velocity-based step: faster scrolling → bigger jumps
            now = time.monotonic()
            dt = now - self._last_scroll_time
            self._last_scroll_time = now
            if dt < 0.03:  # very fast flicking
                step = 12
            elif dt < 0.06:  # fast
                step = 7
            elif dt < 0.12:  # moderate
                step = 4
            else:  # slow / single tick
                step = 3

            # Capture cursor before moving — used as anchor for shift+scroll
            old_pos = self.buffer.cursor_position

            if mouse_event.event_type == MouseEventType.SCROLL_UP:
                new_scroll = max(0, window.vertical_scroll - step)
            else:
                max_line = max(0, self.buffer.document.line_count - 1)
                new_scroll = min(max_line, window.vertical_scroll + step)

            window.vertical_scroll = new_scroll
            # Place cursor on the scroll target line so _scroll() won't override
            new_pos = self.buffer.document.translate_row_col_to_index(new_scroll, 0)
            self.buffer.cursor_position = new_pos

            # Shift+scroll: extend selection from anchor
            if _has_shift():
                if self._drag_start is None:
                    self._drag_start = old_pos  # anchor at pre-scroll position
                a, b = sorted((self._drag_start, new_pos))
                self.sel_start = a
                self.sel_end = b

            try:
                from prompt_toolkit.application import get_app

                get_app().invalidate()
            except Exception:
                pass
            return None

        # --- MOUSE_DOWN — begin drag, do NOT steal focus from input ---
        if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
            pos = self._mouse_pos_to_cursor(
                mouse_event.position.y,
                mouse_event.position.x,
            )
            self._dragging = True

            if _has_shift() and self._drag_start is not None:
                # Shift+click: extend from existing anchor to click point
                a, b = sorted((self._drag_start, pos))
                self.sel_start = a
                self.sel_end = b
            else:
                # Normal click: set new anchor, clear selection
                self._drag_start = pos
                self.sel_start = None
                self.sel_end = None

            try:
                from prompt_toolkit.application import get_app

                get_app().invalidate()
            except Exception:
                pass
            return None

        # --- MOUSE_MOVE — extend selection only while button is held ---
        if mouse_event.event_type == MouseEventType.MOUSE_MOVE:
            if self._dragging and self._drag_start is not None:
                pos = self._mouse_pos_to_cursor(
                    mouse_event.position.y,
                    mouse_event.position.x,
                )
                if pos != self._drag_start:
                    a, b = sorted((self._drag_start, pos))
                    self.sel_start = a
                    self.sel_end = b
                try:
                    from prompt_toolkit.application import get_app

                    get_app().invalidate()
                except Exception:
                    pass
            return None

        # --- MOUSE_UP — auto-copy selection to clipboard ---
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            self._dragging = False

            if self._drag_start is not None:
                pos = self._mouse_pos_to_cursor(
                    mouse_event.position.y,
                    mouse_event.position.x,
                )
                # Fallback: if MOUSE_MOVE never fired, compute range now
                if pos != self._drag_start:
                    a, b = sorted((self._drag_start, pos))
                    self.sel_start = a
                    self.sel_end = b

                # Auto-copy to system clipboard
                if self.sel_start is not None and self.sel_end is not None:
                    selected = self.buffer.text[self.sel_start : self.sel_end]
                    if selected:
                        _copy_to_system_clipboard(selected)

                # _drag_start persists as anchor for shift+click/shift+scroll.
                # Only a non-shift MOUSE_DOWN resets it.

            try:
                from prompt_toolkit.application import get_app

                get_app().invalidate()
            except Exception:
                pass
            return None

        return None


def _copy_to_system_clipboard(text: str) -> None:
    """Best-effort copy to the OS clipboard.

    Tries platform tools first (pbcopy/xclip), then falls back to the
    OSC 52 escape sequence which works across SSH and most modern terminals.
    """
    import base64

    # Platform tool
    try:
        if sys.platform == "darwin":
            proc = subprocess.Popen(
                ["pbcopy"],
                stdin=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            proc.communicate(text.encode("utf-8"), timeout=2)
            if proc.returncode == 0:
                return
        else:
            for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
                try:
                    proc = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                    )
                    proc.communicate(text.encode("utf-8"), timeout=2)
                    if proc.returncode == 0:
                        return
                except FileNotFoundError:
                    continue
    except Exception:
        pass

    # Fallback: OSC 52 (terminal-native clipboard escape)
    try:
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        sys.stdout.write(f"\033]52;c;{encoded}\a")
        sys.stdout.flush()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Session picker state
# ---------------------------------------------------------------------------


class PickerMode(Enum):
    SELECT = "select"  # /sessions: pick one to resume
    MULTI = "multi"  # /archive: checkboxes to archive multiple


@dataclass
class PickerState:
    mode: PickerMode
    items: list[dict[str, Any]]  # session metadata from load_meta()
    cursor: int = 0
    checked: set[int] = field(default_factory=set)
    saved_output: str = ""  # output buffer to restore on dismiss
    include_archived: bool = False


# ---------------------------------------------------------------------------
# Placeholder processor — greyed-out suggestion text in the input bar
# ---------------------------------------------------------------------------


class _PlaceholderProcessor(Processor):
    """Shows greyed-out suggestion text when the input buffer is empty."""

    def __init__(self, get_text):
        self._get_text = get_text

    def apply_transformation(self, ti):  # type: ignore[override]
        if not ti.document.text and ti.lineno == 0:
            placeholder = self._get_text()
            if placeholder:
                return Transformation(
                    fragments=ti.fragments + [("class:placeholder", placeholder)],
                )
        return Transformation(ti.fragments)


# ---------------------------------------------------------------------------
# Suggestion intelligence — context-aware input hints
# ---------------------------------------------------------------------------

# Varied phrasing for "did you mean X?" when a fuzzy match is found.
_COMMAND_SUGGESTION_PHRASES = [
    "Did you mean {cmd}?",
    "Maybe you meant {cmd}?",
    "Perhaps you were looking for {cmd}?",
    "Looks like you might mean {cmd}.",
    "Close match: {cmd} — want to run it?",
    "Not sure about that one — did you mean {cmd}?",
]

# Affirmative responses that accept a pending command suggestion.
_AFFIRMATIVES = frozenset(
    {
        "y",
        "yes",
        "yeah",
        "yep",
        "yup",
        "sure",
        "ok",
        "okay",
        "do it",
        "go",
        "run it",
        "go ahead",
    }
)

# Suggestions keyed by conversation state.
# Each entry is a list to allow rotation on repeated visits.
_SUGGESTIONS: dict[str, list[str]] = {
    "welcome": [
        "Ask me anything, or /help for commands",
    ],
    "after_turn": [
        "Ask a follow-up question...",
        "Try /status, or ask something new",
        "What else would you like to know?",
    ],
    "after_error": [
        "Try rephrasing, or /new for a fresh session",
    ],
    "after_tool": [
        "What would you like to do next?",
    ],
    "after_slash": [
        "Ask me anything, or try another /command",
    ],
    "after_approval": [
        "Continue, or /status to review",
    ],
    "context": [
        "Ask about what you just saw, or /help for commands",
        "What would you like to know more about?",
        "Dig deeper, or try /signal brief for a new briefing",
    ],
}


# ---------------------------------------------------------------------------
# Approval bridge
# ---------------------------------------------------------------------------


@dataclass
class _ApprovalRequest:
    """Bridges the agent thread (which needs a choice) to the main thread."""

    action: Action
    event: threading.Event = field(default_factory=threading.Event)
    choice: str = "r"


# ---------------------------------------------------------------------------
# Interaction modes (mirroring InputProvider._MODES)
# ---------------------------------------------------------------------------

_MODES = ("ask", "plan", "agent")


# ---------------------------------------------------------------------------
# FullScreenChat
# ---------------------------------------------------------------------------


class FullScreenChat:
    """Full-screen TUI for neut chat with fixed input at the bottom."""

    def __init__(
        self,
        agent: ChatAgent,
        store: SessionStore,
        stream: bool = True,
        show_banner: bool = False,
        restart_ctx: dict | None = None,
        auto_picker: bool = False,
    ):
        self._agent = agent
        self._store = store
        self._stream = stream
        self._show_banner = show_banner
        self._restart_ctx = restart_ctx
        self._auto_picker = auto_picker

        # Update system state
        self._update_info: VersionInfo | None = None
        self._update_dismissed = False
        self._pending_update_notification: VersionInfo | None = None
        self._update_checker: BackgroundUpdateChecker | None = None

        # State
        self._busy = False
        self._interrupted = False
        # Default to "ask" mode (read-only, safest) per
        # `prd-axi-cli.md §AXI → Interaction Modes`: AXI always
        # starts in Ask and escalates only with the user's consent.
        # 2026-05-04: previously defaulted to agent which surprised
        # first-touch users — autopilot mode shouldn't be the on-ramp.
        self._mode_idx = _MODES.index("ask")
        self._mode_changed_at: float = 0.0  # monotonic timestamp of last Shift+Tab
        self._spinner_visible = False
        self._spinner_text: FormattedText = FormattedText([])
        self._approval_pending: _ApprovalRequest | None = None
        self._output_lock = threading.Lock()
        self._raw_output = ""  # Raw text without word-wrap breaks
        # Two-buffer tail-only rewrap (T2.2): committed lines never re-wrap.
        self._wrapped_committed = ""       # already-wrapped complete lines
        self._partial_tail = ""            # in-progress line (no trailing \n yet)
        self._table_pending: list[str] = []  # buffered table rows (flush as block)
        # Table-align cache: (content_hash, width) -> aligned lines
        self._align_table_cache: dict[tuple[str, int], list[str]] = {}
        # Invalidate throttle: at most one app.invalidate() per 50ms
        self._last_invalidate: float = 0.0
        self._last_model = ""
        self._last_tokens = ""
        self._last_cost = ""
        # T2.5 — inflight streaming token counters (live status line)
        self._inflight_tokens_in: int = 0
        self._inflight_tokens_out: int = 0
        self._inflight_started_at: float | None = None
        # T2.6 — separate thinking buffer + open/closed toggle
        self._thinking_buffer: str = ""
        self._thinking_open: bool = False
        self._picker: PickerState | None = None

        # Suggestion state — drives placeholder text in input bar
        self._suggestion_key = "welcome"
        self._suggestion_idx = 0

        # Pending command suggestion — set when a fuzzy match is offered
        self._pending_command: str | None = None

        # Input history (shell-style up/down cycling)
        self._input_history: list[str] = []
        self._history_idx: int = 0  # points past end when not browsing
        self._history_stash: str = ""  # saves in-progress text when browsing

        # Mermaid rendering — managed by TIDY, auto-cleaned on process exit
        self._mermaid_cache: dict[str, str | None] = {}
        self._mermaid_dir: Path | None = None
        if _MMDC_PATH:
            self._mermaid_dir = acquire_dir("chat.mermaid", purpose="diagram SVG renders")

        # Sci Displays math rendering — content-addressed cache per spec §6.4.
        # Cache key = original LaTeX source (the underlying math_render module
        # writes to a content-hashed file path under $AXI_STATE_DIR).
        self._math_cache: dict[str, str | None] = {}

        # Scroll metrics (captured by _ScrollMetricsCapture margin on output window)
        self._scroll_content_height = 0
        self._scroll_window_height = 0
        self._scroll_vertical_scroll = 0
        self._scroll_window_width = 0

        # T3.12B — new-content-below indicator
        self._user_scrolled_up: bool = False
        self._new_lines_below: int = 0

        # Spinner state (updated from spinner thread)
        self._spinner_label = "Thinking"
        self._spinner_sub_state = ""
        self._spinner_input_tokens = 0
        self._spinner_output_tokens = 0
        self._spinner_start: float = 0.0
        self._spinner_stop_event = threading.Event()
        self._spinner_thread: threading.Thread | None = None

        # Build UI
        self._output_buffer = Buffer(read_only=True, name="output")

        from .commands import get_slash_commands

        slash_commands = list(get_slash_commands().keys())
        self._input_buffer = Buffer(
            name="input",
            accept_handler=self._on_accept,
            multiline=True,
            completer=WordCompleter(slash_commands, sentence=True),
        )
        self._app = self._build_app()

    # -- Layout & keybindings ------------------------------------------------

    def _build_app(self) -> Application:
        kb = KeyBindings()
        no_picker = Condition(lambda: self._picker is None)

        @kb.add("c-d")
        def _exit(event):
            event.app.exit()

        @kb.add("escape", "escape")
        def _double_escape(event):
            """Double-Esc: dismiss picker, interrupt, clear input, or exit."""
            if self._picker is not None:
                self._dismiss_picker()
                return
            if self._busy:
                self._interrupted = True
                self._agent.cancel()
            elif self._input_buffer.text:
                self._input_buffer.reset()
            else:
                event.app.exit()

        @kb.add("s-tab")
        def _cycle_mode(event):
            if self._picker is not None:
                return
            self._mode_idx = (self._mode_idx + 1) % len(_MODES)
            self._mode_changed_at = time.monotonic()
            # Propagate to the agent so the next turn actually behaves
            # differently — not just a relabeled toolbar.
            try:
                self._agent.set_interaction_mode(_MODES[self._mode_idx])
            except Exception:
                pass
            event.app.invalidate()
            # Schedule revert paint after 250ms flash window
            def _revert_flash():
                time.sleep(0.25)
                event.app.invalidate()
            threading.Thread(target=_revert_flash, daemon=True).start()

        # Multiline: Enter submits, Alt+Enter inserts newline
        @kb.add("enter", filter=no_picker)
        def _submit_input(event):
            event.current_buffer.validate_and_handle()

        @kb.add("escape", "enter", filter=no_picker)
        def _newline_input(event):
            event.current_buffer.insert_text("\n")

        # -- macOS text navigation --
        # Cmd+Left / Cmd+Right — line start/end
        # (Terminal sends escape sequences for Cmd+arrows)
        @kb.add("home")
        @kb.add("c-a")
        def _line_start(event):
            buf = event.current_buffer
            buf.selection_state = None
            doc = buf.document
            buf.cursor_position = doc.cursor_position - len(doc.current_line_before_cursor)

        @kb.add("end")
        @kb.add("c-e")
        def _line_end(event):
            buf = event.current_buffer
            buf.selection_state = None
            doc = buf.document
            buf.cursor_position = doc.cursor_position + len(doc.current_line_after_cursor)

        # Option+Left / Option+Right — word jump
        # (Terminal sends escape+b / escape+f for Option+arrows)
        @kb.add("escape", "b")
        def _word_left(event):
            buf = event.current_buffer
            buf.selection_state = None
            pos = buf.document.find_previous_word_beginning() or 0
            buf.cursor_position += pos

        @kb.add("escape", "f")
        def _word_right(event):
            buf = event.current_buffer
            buf.selection_state = None
            pos = buf.document.find_next_word_ending() or 0
            buf.cursor_position += pos

        # Alt+T (escape, t) / Ctrl+T — toggle thinking-block expand (T2.6)
        @kb.add("escape", "t")
        @kb.add("c-t")
        def _toggle_thinking(event):
            self._thinking_open = not self._thinking_open
            event.app.invalidate()

        # Option+Backspace — delete word backward
        @kb.add("escape", "c-h")
        def _delete_word_back(event):
            buf = event.current_buffer
            pos = buf.document.find_previous_word_beginning() or 0
            if pos:
                buf.delete_before_cursor(count=-pos)

        # Cmd+Backspace — delete to line start
        @kb.add("c-u")
        def _delete_to_start(event):
            buf = event.current_buffer
            before = len(buf.document.current_line_before_cursor)
            if before:
                buf.delete_before_cursor(count=before)

        # Ctrl+K — delete to line end
        @kb.add("c-k")
        def _delete_to_end(event):
            buf = event.current_buffer
            after = len(buf.document.current_line_after_cursor)
            if after:
                buf.delete(count=after)

        # Option+D — delete word forward
        @kb.add("escape", "d")
        def _delete_word_forward(event):
            buf = event.current_buffer
            pos = buf.document.find_next_word_ending() or 0
            if pos:
                buf.delete(count=pos)

        # -- Selection (Shift+arrow, Shift+Option+arrow, Shift+Cmd) --

        def _start_or_extend_selection(buf, selection_type=SelectionType.CHARACTERS):
            if buf.selection_state is None:
                buf.selection_state = SelectionState(
                    original_cursor_position=buf.cursor_position,
                    type=selection_type,
                )

        # Shift+Left / Shift+Right — character selection
        @kb.add("s-left")
        def _sel_left(event):
            buf = event.current_buffer
            _start_or_extend_selection(buf)
            buf.cursor_position -= 1

        @kb.add("s-right")
        def _sel_right(event):
            buf = event.current_buffer
            _start_or_extend_selection(buf)
            buf.cursor_position += 1

        # Shift+Up / Shift+Down — extend selection one line at a time
        # On first line: select to start. On last line: select to end.
        @kb.add("s-up")
        def _sel_up(event):
            buf = event.current_buffer
            _start_or_extend_selection(buf)
            if buf.document.cursor_position_row == 0:
                buf.cursor_position = 0
            else:
                buf.cursor_up()

        @kb.add("s-down")
        def _sel_down(event):
            buf = event.current_buffer
            _start_or_extend_selection(buf)
            if buf.document.cursor_position_row >= buf.document.line_count - 1:
                buf.cursor_position = len(buf.text)
            else:
                buf.cursor_down()

        # Shift+Option+Left/Right — word selection
        # macOS Terminal sends \x1b[1;4D / \x1b[1;4C (Alt+Shift+arrow)
        # which prompt_toolkit parses as escape + s-left / s-right.
        # Also bind Esc+B/F for terminals that send those instead.
        @kb.add("escape", "s-left")
        @kb.add("escape", "B")
        def _sel_word_left(event):
            buf = event.current_buffer
            _start_or_extend_selection(buf)
            pos = buf.document.find_previous_word_beginning() or 0
            buf.cursor_position += pos

        @kb.add("escape", "s-right")
        @kb.add("escape", "F")
        def _sel_word_right(event):
            buf = event.current_buffer
            _start_or_extend_selection(buf)
            pos = buf.document.find_next_word_ending() or 0
            buf.cursor_position += pos

        # Shift+Home — select to line start
        @kb.add("s-home")
        def _sel_line_start(event):
            buf = event.current_buffer
            _start_or_extend_selection(buf)
            before = len(buf.document.current_line_before_cursor)
            buf.cursor_position -= before

        # Shift+End — select to line end
        @kb.add("s-end")
        def _sel_line_end(event):
            buf = event.current_buffer
            _start_or_extend_selection(buf)
            after = len(buf.document.current_line_after_cursor)
            buf.cursor_position += after

        # Ctrl+Shift+A — select all (Cmd+A in most terminals)
        @kb.add("c-a", "c-a")  # double tap as fallback
        def _sel_all_double(event):
            buf = event.current_buffer
            buf.cursor_position = 0
            buf.selection_state = SelectionState(
                original_cursor_position=0,
                type=SelectionType.CHARACTERS,
            )
            buf.cursor_position = len(buf.text)

        # Plain movement clears selection (filtered out when picker is active)
        @kb.add("left", filter=no_picker)
        def _left(event):
            buf = event.current_buffer
            buf.selection_state = None
            buf.cursor_position -= 1

        @kb.add("right", filter=no_picker)
        def _right(event):
            buf = event.current_buffer
            buf.selection_state = None
            buf.cursor_position += 1

        @kb.add("up", filter=no_picker)
        def _up(event):
            buf = event.current_buffer
            buf.selection_state = None
            # History cycling: if on first line, go to previous history entry
            if buf.document.cursor_position_row == 0:
                if self._input_history and self._history_idx > 0:
                    # Stash current text on first browse
                    if self._history_idx == len(self._input_history):
                        self._history_stash = buf.text
                    self._history_idx -= 1
                    entry = self._input_history[self._history_idx]
                    buf.set_document(Document(entry, len(entry)), bypass_readonly=False)
                return
            buf.cursor_up()

        @kb.add("down", filter=no_picker)
        def _down(event):
            buf = event.current_buffer
            buf.selection_state = None
            # History cycling: if on last line, go to next history entry
            if buf.document.cursor_position_row >= buf.document.line_count - 1:
                if self._history_idx < len(self._input_history):
                    self._history_idx += 1
                    if self._history_idx == len(self._input_history):
                        # Restore stashed in-progress text
                        entry = self._history_stash
                    else:
                        entry = self._input_history[self._history_idx]
                    buf.set_document(Document(entry, len(entry)), bypass_readonly=False)
                return
            buf.cursor_down()

        # -- Clipboard (Ctrl+C/V/X map to system clipboard) --
        # Note: Ctrl+C is already bound to cancel above, so we use it
        # only when there IS a selection (copy), otherwise cancel.

        # Override Ctrl+C: copy if selection exists, otherwise cancel.
        # Checks the output control's sel_start/sel_end (mouse drag) and
        # the input buffer's selection_state (shift+arrow selection).
        @kb.add("c-c", eager=True)
        def _copy_or_cancel(event):
            # Check output area mouse selection first
            ctrl = self._output_window.content
            if (
                hasattr(ctrl, "sel_start")
                and ctrl.sel_start is not None  # type: ignore[union-attr]
                and ctrl.sel_end is not None
            ):  # type: ignore[union-attr]
                selected_text = self._output_buffer.text[ctrl.sel_start : ctrl.sel_end]  # type: ignore[union-attr]
                if selected_text:
                    event.app.clipboard.set_data(ClipboardData(selected_text))
                    _copy_to_system_clipboard(selected_text)
                ctrl.sel_start = None  # type: ignore[union-attr]
                ctrl.sel_end = None  # type: ignore[union-attr]
                event.app.invalidate()
                return
            # Check input buffer selection (shift+arrow)
            buf = event.current_buffer
            if buf.selection_state is not None:
                start = buf.selection_state.original_cursor_position
                end = buf.cursor_position
                if start > end:
                    start, end = end, start
                if start != end:
                    selected_text = buf.text[start:end]
                    event.app.clipboard.set_data(ClipboardData(selected_text))
                    _copy_to_system_clipboard(selected_text)
                buf.selection_state = None
                return
            # No selection — original cancel behavior
            if self._picker is not None:
                self._dismiss_picker()
            elif self._busy:
                self._interrupted = True
                self._agent.cancel()
            else:
                self._input_buffer.reset()

        # Ctrl+V — paste
        @kb.add("c-v", eager=True)
        def _paste(event):
            data = event.app.clipboard.get_data()
            if data.text:
                event.current_buffer.insert_text(data.text)

        # Ctrl+X — cut
        @kb.add("c-x", eager=True)
        def _cut(event):
            buf = event.current_buffer
            if buf.selection_state is not None:
                start = buf.selection_state.original_cursor_position
                end = buf.cursor_position
                if start > end:
                    start, end = end, start
                if start != end:
                    selected_text = buf.text[start:end]
                    event.app.clipboard.set_data(ClipboardData(selected_text))
                    _delete_selection(buf)
                else:
                    buf.selection_state = None
            # No selection → no-op (don't exit or anything drastic)

        # Backspace/Delete with selection — delete selected text
        def _delete_selection(buf):
            """Remove selected text manually (more reliable than cut_selection)."""
            if buf.selection_state is None:
                return False
            start = buf.selection_state.original_cursor_position
            end = buf.cursor_position
            if start > end:
                start, end = end, start
            if start == end:
                buf.selection_state = None
                return False
            new_text = buf.text[:start] + buf.text[end:]
            buf.selection_state = None
            buf.set_document(Document(new_text, start), bypass_readonly=False)
            return True

        @kb.add("backspace", eager=True)
        def _backspace(event):
            buf = event.current_buffer
            if not _delete_selection(buf):
                buf.delete_before_cursor(count=1)

        @kb.add("delete", eager=True)
        def _delete(event):
            buf = event.current_buffer
            if not _delete_selection(buf):
                buf.delete(count=1)

        # -- Output scrolling (Page Up/Down while focus stays on input) --

        def _scroll_output(lines: int) -> None:
            """Scroll the output buffer cursor by `lines` (negative=up)."""
            buf = self._output_buffer
            doc = buf.document
            target_row = max(
                0,
                min(
                    doc.cursor_position_row + lines,
                    doc.line_count - 1,
                ),
            )
            new_pos = doc.translate_row_col_to_index(target_row, 0)
            buf.set_document(
                Document(doc.text, new_pos),
                bypass_readonly=True,
            )

        @kb.add("pageup", filter=no_picker)
        def _page_up(event):
            _scroll_output(-20)

        @kb.add("pagedown", filter=no_picker)
        def _page_down(event):
            _scroll_output(20)

        @kb.add("s-up", filter=no_picker)
        def _scroll_up_line(event):
            _scroll_output(-3)

        @kb.add("s-down", filter=no_picker)
        def _scroll_down_line(event):
            _scroll_output(3)

        # -- Picker keybindings (active only when picker is open) --
        picker_kb = KeyBindings()

        @picker_kb.add("up")
        def _picker_up(event):
            p = self._picker
            if p and p.cursor > 0:
                p.cursor -= 1
                self._render_picker()

        @picker_kb.add("down")
        def _picker_down(event):
            p = self._picker
            if p and p.cursor < len(p.items) - 1:
                p.cursor += 1
                self._render_picker()

        @picker_kb.add(" ")
        def _picker_toggle(event):
            p = self._picker
            if p and p.mode == PickerMode.MULTI:
                if p.cursor in p.checked:
                    p.checked.discard(p.cursor)
                else:
                    p.checked.add(p.cursor)
                self._render_picker()

        @picker_kb.add("enter")
        def _picker_confirm(event):
            if self._picker is not None:
                self._confirm_picker()

        @picker_kb.add("tab")
        def _picker_toggle_archived(event):
            if self._picker is not None:
                self._toggle_picker_archived()

        conditional_picker = ConditionalKeyBindings(
            picker_kb,
            filter=Condition(lambda: self._picker is not None),
        )
        # Picker bindings first so they take priority over main up/down/enter
        combined_kb = merge_key_bindings([conditional_picker, kb])

        # Output area — fills ALL remaining vertical space, pushing
        # the fixed-height bottom elements to the terminal floor.
        output_control = _ScrollableBufferControl(
            tui=self,
            buffer=self._output_buffer,
            focusable=True,
            lexer=_OutputLexer(),  # lexer wired to control below
        )
        # Wire lexer → control so it can read sel_start/sel_end
        output_control.lexer = _OutputLexer(control=output_control)
        output_window = Window(
            content=output_control,
            wrap_lines=True,
            height=D(min=1, weight=1),
            right_margins=[_ScrollMetricsCapture(self)],
        )
        self._output_window = output_window

        # Spinner bar — visible only when busy
        spinner_bar = ConditionalContainer(
            content=Window(
                content=FormattedTextControl(lambda: self._spinner_text),
                height=1,
            ),
            filter=Condition(lambda: self._spinner_visible),
        )

        # T2.6 — thinking placeholder (collapsed) shown when thinking content arrived
        thinking_placeholder = ConditionalContainer(
            content=Window(
                content=FormattedTextControl(self._get_thinking_placeholder_text),
                height=1,
            ),
            filter=Condition(
                lambda: bool(self._thinking_buffer) and not self._thinking_open
            ),
        )

        # T2.6 — thinking expanded view
        thinking_expanded = ConditionalContainer(
            content=Window(
                content=FormattedTextControl(self._get_thinking_expanded_text),
                height=D(min=1, max=10),
                wrap_lines=True,
            ),
            filter=Condition(
                lambda: bool(self._thinking_buffer) and self._thinking_open
            ),
        )

        # Horizontal border
        border = Window(
            content=FormattedTextControl(self._get_border_text),
            height=1,
        )

        # Input area with "you>" prompt and contextual placeholder
        input_window = Window(
            content=BufferControl(
                buffer=self._input_buffer,
                input_processors=[
                    BeforeInput(self._get_input_prefix),
                    _PlaceholderProcessor(self._get_suggestion),
                ],
            ),
            height=D(min=1, max=20),
            wrap_lines=True,
            dont_extend_height=True,
            get_line_prefix=self._get_input_line_prefix,
        )

        # Toolbar — mode switcher
        toolbar = Window(
            content=FormattedTextControl(self._get_toolbar_text),
            height=1,
        )

        # Persistent status line — model, tokens, cost
        status_line = Window(
            content=FormattedTextControl(self._get_status_text),
            height=1,
        )

        # Bottom border — closes the input frame
        bottom_border = Window(
            content=FormattedTextControl(self._get_border_text),
            height=1,
        )

        main_column = HSplit(
            [
                output_window,
                spinner_bar,
                thinking_placeholder,   # T2.6 — collapsed
                thinking_expanded,      # T2.6 — expanded
                status_line,
                border,
                input_window,
                bottom_border,
                toolbar,
                Window(height=1),  # padding below toolbar
            ]
        )

        # Full-height scrollbar column at the rightmost edge of the terminal.
        # Uses scroll metrics captured by _ScrollMetricsCapture on the output window.
        scrollbar_column = Window(
            content=FormattedTextControl(self._get_scrollbar_fragments),
            width=2,
        )

        scrollbar_gutter = Window(width=1)  # padding between content and scrollbar
        root_split = VSplit([main_column, scrollbar_gutter, scrollbar_column])

        self._picker_float = Float(
            content=ConditionalContainer(
                content=Window(
                    content=FormattedTextControl(self._picker_float_content),
                    dont_extend_height=False,
                ),
                filter=Condition(lambda: self._picker is not None),
            ),
            left=0,
            top=0,
            right=0,
            bottom=4,
        )
        root = FloatContainer(
            content=root_split,
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=8),
                ),
                self._picker_float,
            ],
        )

        layout = Layout(root, focused_element=input_window)

        return Application(
            layout=layout,
            key_bindings=combined_kb,
            full_screen=True,
            mouse_support=True,
            style=_STYLE,
        )

    def _get_input_prefix(self) -> list[tuple[str, str]]:
        """Dynamic input prompt: changes during approval/picker mode."""
        if self._picker is not None:
            return [("", "")]
        if self._approval_pending:
            return [("class:warning", "  > ")]
        return [("class:prompt", "you> ")]

    def _get_input_line_prefix(self, lineno: int, wrap_count: int) -> list[tuple[str, str]]:
        """Continuation-line prefix for multi-line input (line 1+)."""
        if lineno == 0 or self._picker is not None or self._approval_pending:
            return [("", "")]
        return [("class:dim", "··· ")]

    def _get_border_text(self) -> FormattedText:
        try:
            width = self._app.output.get_size().columns - 3  # scrollbar + gutter
        except Exception:
            width = 77
        return FormattedText([("class:border", "\u2500" * width)])

    def _get_toolbar_text(self) -> FormattedText:
        if self._picker is not None:
            if self._picker.mode == PickerMode.MULTI:
                return FormattedText(
                    [
                        (
                            "class:toolbar.dim",
                            " \u2191\u2193 navigate  \u00b7  Space toggle"
                            "  \u00b7  Tab archives"
                            "  \u00b7  Enter confirm  \u00b7  Esc\u00b7Esc back",
                        ),
                    ]
                )
            return FormattedText(
                [
                    (
                        "class:toolbar.dim",
                        " \u2191\u2193 navigate  \u00b7  Tab archives"
                        "  \u00b7  Enter to load"
                        "  \u00b7  Esc\u00b7Esc back",
                    ),
                ]
            )
        mode = _MODES[self._mode_idx]
        flash_active = time.monotonic() - self._mode_changed_at < 0.25
        mode_class = "class:warning" if flash_active else "class:toolbar.mode"
        parts: list[tuple[str, str]] = [
            ("class:toolbar.arrow", " \u23f5\u23f5 "),
            (mode_class, f"{mode} mode"),
            ("class:toolbar.dim", "  (shift+tab to switch)"),
            (
                "class:toolbar.dim",
                "  \u00b7  option+return for newline"
                if sys.platform == "darwin"
                else "  \u00b7  alt+enter for newline",
            ),
        ]
        if self._busy:
            parts.append(("class:toolbar.dim", "  \u00b7  esc to interrupt"))
        if self._approval_pending:
            parts.append(
                ("class:toolbar.approval", "  \u00b7  [a]pprove [A]lways [r]eject [s]kip"),
            )
        return FormattedText(parts)

    def _get_status_text(self) -> FormattedText:
        """Persistent status bar: scroll pos (left) · model · tokens · ctx% (right)."""
        from axiom.infra.text_utils import pluralize

        # Scroll position indicator (left side)
        buf = self._output_buffer
        total = buf.document.line_count
        at_end = buf.cursor_position >= len(buf.text) - 1

        # Track scrolled-up state
        self._user_scrolled_up = total > 1 and not at_end

        if total > 1 and not at_end:
            row = buf.document.cursor_position_row
            pct = int(row * 100 / max(total - 1, 1))
            new_below = getattr(self, "_new_lines_below", 0)
            if new_below > 0:
                scroll_hint = (
                    f" \u2193 {pluralize(new_below, 'new line')} below \u00b7 End to jump"
                )
            else:
                scroll_hint = f" \u2191 {pct}%"
        else:
            self._new_lines_below = 0
            scroll_hint = ""

        # Right-side pieces: update · model · tokens · cost · ctx%
        pieces: list[str] = []
        if self._update_info and self._update_info.is_newer and not self._update_dismissed:
            pieces.append("\u2191 update")
        if self._last_model:
            pieces.append(self._last_model)

        # T2.5 — show inflight counters while streaming, last-turn otherwise
        if self._busy and (self._inflight_tokens_in or self._inflight_tokens_out):
            pieces.append(
                f"{self._inflight_tokens_in}in/{self._inflight_tokens_out}out • streaming…"
            )
        elif self._last_tokens:
            pieces.append(self._last_tokens)
            if self._last_cost:
                pieces.append(self._last_cost)

        # T2.5 — context-window % always visible
        try:
            msgs = getattr(self._agent, "session", None)
            msgs = getattr(msgs, "messages", []) if msgs else []
            total_chars = sum(len(m.content or "") for m in msgs)
            ctx_pct = min(99, int(total_chars * 100 // (128_000 * 4)))
            pieces.append(f"{ctx_pct}% ctx")
        except Exception:
            pass

        right = " \u00b7 ".join(pieces) if pieces else ""
        try:
            width = self._app.output.get_size().columns - 3  # scrollbar + gutter
        except Exception:
            width = 77

        gap = max(width - len(scroll_hint) - len(right), 1)
        return FormattedText(
            [
                ("class:toolbar.dim", scroll_hint),
                ("class:status", " " * gap + right),
            ]
        )

    # -- T2.6 Thinking-block helpers -------------------------------------------

    def _get_thinking_placeholder_text(self) -> FormattedText:
        """One-line placeholder shown when thinking content is collapsed."""
        n_chars = len(self._thinking_buffer)
        est_tokens = max(1, n_chars // 4)
        return FormattedText(
            [
                ("class:dim", f"  ▶ [thinking] {est_tokens} tokens · Alt+T to expand"),
            ]
        )

    def _get_thinking_expanded_text(self) -> FormattedText:
        """Expanded thinking content (max 10 lines, styled dim)."""
        lines = self._thinking_buffer.splitlines()
        parts: list[tuple[str, str]] = []
        for line in lines[:40]:
            parts.append(("class:dim", f"  {line}\n"))
        if len(lines) > 40:
            parts.append(("class:dim", f"  … ({len(lines) - 40} more lines) · Alt+T to collapse\n"))
        else:
            parts.append(("class:dim", "  Alt+T to collapse\n"))
        return FormattedText(parts)

    # -- Scrollbar column content -----------------------------------------------

    def _get_scrollbar_fragments(self) -> FormattedText:
        """Generate the full-height scrollbar column content.

        Reads scroll metrics captured by _ScrollMetricsCapture on the output
        window. Returns a 2-char-wide column: mostly empty (transparent) with
        a small solid thumb block when the output content overflows.
        """
        ch = self._scroll_content_height
        wh = self._scroll_window_height
        vs = self._scroll_vertical_scroll

        try:
            rows = self._app.output.get_size().rows
        except Exception:
            rows = 24

        # No scrollbar needed if content fits in the output window
        if ch <= wh or wh <= 0:
            parts: list[tuple[str, str]] = []
            for i in range(rows):
                parts.append(("", "  "))
                if i < rows - 1:
                    parts.append(("", "\n"))
            return FormattedText(parts)

        # Thumb size: small indicator (1/6 of proportional size, min 2 rows)
        ratio = wh / ch
        thumb_size = max(2, int(ratio * rows / 6))

        # Thumb position: proportional to scroll offset
        max_scroll = max(ch - wh, 1)
        scroll_ratio = vs / max_scroll
        max_pos = max(rows - thumb_size, 0)
        pos = int(scroll_ratio * max_pos)
        pos = max(0, min(pos, max_pos))

        parts = []
        for i in range(rows):
            if pos <= i < pos + thumb_size:
                parts.append(("class:scrollbar.thumb", "  "))
            else:
                parts.append(("", "  "))
            if i < rows - 1:
                parts.append(("", "\n"))
        return FormattedText(parts)

    # -- Suggestion intelligence ----------------------------------------------

    def _get_suggestion(self) -> str:
        """Return the current contextual suggestion for the input placeholder."""
        if self._picker is not None:
            return ""
        if self._approval_pending:
            return "Type a/A/r/s to respond to the approval prompt"
        if self._pending_command:
            return f'Type "yes" to run {self._pending_command}, or keep typing'
        if self._busy:
            return ""
        entries = _SUGGESTIONS.get(self._suggestion_key, [])
        if not entries:
            return ""
        return entries[self._suggestion_idx % len(entries)]

    def _set_suggestion(self, key: str) -> None:
        """Advance the suggestion state. Rotates within a category."""
        if key == self._suggestion_key:
            entries = _SUGGESTIONS.get(key, [])
            if len(entries) > 1:
                self._suggestion_idx = (self._suggestion_idx + 1) % len(entries)
        else:
            self._suggestion_key = key
            self._suggestion_idx = 0

    # -- Thread-safe output --------------------------------------------------

    def _get_wrap_width(self) -> int:
        """Terminal width available for output text (minus scrollbar + gutter)."""
        try:
            return self._app.output.get_size().columns - 4  # 2 scrollbar + 1 gutter + 1 margin
        except Exception:
            return 76

    def _align_table_cached(self, table_lines: list[str], width: int) -> list[str]:
        """Call _align_table with an instance-level cache keyed on (content, width)."""
        key = ("\n".join(table_lines), width)
        if key not in self._align_table_cache:
            self._align_table_cache[key] = _align_table(table_lines, width)
        return self._align_table_cache[key]

    def _word_wrap(self, text: str, width: int) -> str:
        """Word-wrap complete lines, preserving tables and indentation."""
        lines = text.split("\n")
        result: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]

            # --- Table block: collect consecutive rows, align columns ---
            if _RE_TABLE_ROW.match(line) or _RE_TABLE_SEP.match(line):
                table_lines: list[str] = []
                while i < len(lines) and (
                    _RE_TABLE_ROW.match(lines[i]) or _RE_TABLE_SEP.match(lines[i])
                ):
                    table_lines.append(lines[i])
                    i += 1
                result.extend(self._align_table_cached(table_lines, width))
                continue

            # --- Regular line ---
            if len(line) <= width or not line.strip():
                result.append(line)
            else:
                stripped = line.lstrip()
                indent = line[: len(line) - len(stripped)]
                # width is the total line width; textwrap accounts for
                # the indent within that, so pass the full width.
                wrapped = textwrap.fill(
                    stripped,
                    width=max(width, 20),
                    initial_indent=indent,
                    subsequent_indent=indent + "  ",
                    break_long_words=False,
                    break_on_hyphens=False,
                )
                result.append(wrapped)
            i += 1
        return "\n".join(result)

    def _append_output(self, text: str) -> None:
        """Append text to the output buffer, word-wrapped (thread-safe).

        Uses a two-buffer approach so already-committed lines are never
        re-wrapped: _wrapped_committed holds finished lines; _partial_tail
        accumulates the current in-flight line. Only the tail is re-wrapped
        per chunk, giving O(N) total work instead of O(N²).
        """
        with self._output_lock:
            width = self._get_wrap_width()
            old_len = len(self._output_buffer.text)
            old_cursor = self._output_buffer.cursor_position
            following = old_cursor >= old_len - 1

            self._raw_output += text
            self._partial_tail += text

            # Flush complete lines from the tail into committed storage.
            # Table rows are buffered in _table_pending and flushed as a block
            # so _align_table_cached can cache the whole table, not individual rows.
            while "\n" in self._partial_tail:
                line, _, self._partial_tail = self._partial_tail.partition("\n")
                if _RE_TABLE_ROW.match(line) or _RE_TABLE_SEP.match(line):
                    self._table_pending.append(line)
                else:
                    if self._table_pending:
                        block = "\n".join(self._table_pending)
                        self._wrapped_committed += self._word_wrap(block, width) + "\n"
                        self._table_pending = []
                    self._wrapped_committed += self._word_wrap(line, width) + "\n"

            full_text = self._wrapped_committed + self._partial_tail
            if following:
                cursor = len(full_text)
            else:
                cursor = min(old_cursor, len(full_text))
            self._output_buffer.set_document(
                Document(full_text, cursor),
                bypass_readonly=True,
            )

        # Throttle: at most one invalidate() per 50 ms
        now = time.monotonic()
        if now - self._last_invalidate >= 0.05:
            self._last_invalidate = now
            self._app.invalidate()

    def _rewrap_buffer(self) -> None:
        """Re-word-wrap the entire output buffer from raw text.

        Called after streaming completes and on terminal resize. Rebuilds
        _wrapped_committed and _partial_tail from scratch so column widths
        match the current terminal width. Also invalidates the align cache.
        """
        with self._output_lock:
            width = self._get_wrap_width()
            if not self._raw_output:
                return
            self._align_table_cache.clear()
            old_len = len(self._output_buffer.text)
            old_cursor = self._output_buffer.cursor_position
            following = old_cursor >= old_len - 1
            # Rebuild from raw using table-block buffering (same logic as _append_output)
            self._wrapped_committed = ""
            self._partial_tail = self._raw_output
            self._table_pending = []
            while "\n" in self._partial_tail:
                line, _, self._partial_tail = self._partial_tail.partition("\n")
                if _RE_TABLE_ROW.match(line) or _RE_TABLE_SEP.match(line):
                    self._table_pending.append(line)
                else:
                    if self._table_pending:
                        block = "\n".join(self._table_pending)
                        self._wrapped_committed += self._word_wrap(block, width) + "\n"
                        self._table_pending = []
                    self._wrapped_committed += self._word_wrap(line, width) + "\n"
            if self._table_pending:
                block = "\n".join(self._table_pending)
                self._wrapped_committed += self._word_wrap(block, width) + "\n"
                self._table_pending = []
            full_text = self._wrapped_committed + self._partial_tail
            if full_text != self._output_buffer.text:
                cursor = len(full_text) if following else min(old_cursor, len(full_text))
                self._output_buffer.set_document(
                    Document(full_text, cursor),
                    bypass_readonly=True,
                )
        self._app.invalidate()

    def _process_mermaid(self) -> None:
        """Scan raw output for mermaid blocks, render to SVG, replace with placeholders."""
        if not self._mermaid_dir:
            return
        with self._output_lock:
            updated = _process_mermaid_blocks(
                self._raw_output,
                self._mermaid_cache,
                self._mermaid_dir,
            )
            if updated != self._raw_output:
                self._raw_output = updated
        # Rewrap with the updated raw text (placeholder lines replace code blocks)
        self._rewrap_buffer()

    def _process_math(self) -> None:
        """Scan raw output for ```math blocks, render via Sci Displays,
        replace each with a single-line placeholder. Mirror of
        ``_process_mermaid`` for math equations."""
        with self._output_lock:
            updated = _process_math_blocks(self._raw_output, self._math_cache)
            if updated != self._raw_output:
                self._raw_output = updated
        self._rewrap_buffer()

    # -- Input accept handler ------------------------------------------------

    def _on_accept(self, buff: Buffer) -> bool:
        """Called when user presses Enter in the input buffer.

        Returns True to keep text in buffer, False to clear it.
        """
        # Picker handles Enter via its own keybinding
        if self._picker is not None:
            return True

        # Snap to bottom so new response auto-scrolls
        buf = self._output_buffer
        buf.set_document(Document(buf.text, len(buf.text)), bypass_readonly=True)
        text = buff.text.strip()
        if not text:
            return True  # no-op, keep empty buffer

        # Record in input history (avoid consecutive duplicates)
        if not self._input_history or self._input_history[-1] != text:
            self._input_history.append(text)
        self._history_idx = len(self._input_history)
        self._history_stash = ""

        # Support """ wrapping as alternative multiline delimiter
        if text.startswith('"""') and text.endswith('"""') and len(text) > 6:
            text = text[3:-3].strip()
            if not text:
                return True

        # If we're waiting for an approval response
        if self._approval_pending:
            self._handle_approval_input(text)
            return False

        # If we offered a command suggestion, check for affirmative
        if self._pending_command:
            if self._check_affirmative(text):
                return False  # executed the suggestion
            # Not affirmative — clear pending and fall through to normal flow
            self._pending_command = None

        # Don't allow input while agent is working
        if self._busy:
            return True  # keep text so user can send it later

        # Slash commands
        if text.startswith("/"):
            self._handle_slash_command(text)
            return False

        # Legacy exit
        if text.lower() in ("exit", "quit"):
            self._app.exit()
            return False

        # Normal chat — paint spinner in main thread, then spawn agent thread
        self._busy = True
        self._interrupted = False
        self._append_output(f"you> {text}\n\n")
        # Start spinner synchronously so the first frame appears in the same
        # render tick as the prompt clearing — kills ~50ms dead-air.
        self._start_spinner("Thinking")

        t = threading.Thread(
            target=self._run_agent_turn,
            args=(text,),
            daemon=True,
        )
        t.start()
        return False

    # -- Slash commands ------------------------------------------------------

    def _handle_slash_command(self, text: str) -> None:
        parts = text.split()
        cmd = parts[0].lower()

        if cmd in ("/exit", "/quit"):
            self._app.exit()
            return

        # /sessions with subcommands
        if cmd == "/sessions":
            if len(parts) == 1:
                self._open_picker(PickerMode.SELECT)
                return
            subcmd = parts[1].lower()
            if subcmd == "rename":
                from .commands import cmd_rename

                title = " ".join(parts[2:]).strip()
                result = cmd_rename(self._agent, self._store, title)
                self._append_output(_strip_ansi(result) + "\n")
                self._set_suggestion("after_slash")
                return
            if subcmd == "archive":
                if len(parts) == 2:
                    self._open_picker(PickerMode.MULTI)
                    return
                from .commands import cmd_archive

                arg = parts[2].strip()
                result = cmd_archive(arg, self._store, self._agent)
                self._append_output(_strip_ansi(result) + "\n")
                self._set_suggestion("after_slash")
                return
            # Unknown subcommand
            self._append_output(f"\n  Unknown: /sessions {subcmd}\n\n")
            self._set_suggestion("after_slash")
            return

        # Backward compat: bare /archive (no args) opens multi-picker
        if cmd == "/archive" and len(parts) == 1:
            self._open_picker(PickerMode.MULTI)
            return

        # Backward compat: /rename and /archive with args fall through
        # to cli._handle_slash_command below (skip unknown-command check)
        if cmd in ("/rename", "/archive"):
            from .cli import _handle_slash_command

            result = _handle_slash_command(text, self._agent, self._store)
            if result == "exit":
                self._app.exit()
                return
            if result:
                self._append_output(_strip_ansi(result) + "\n")
            self._set_suggestion("after_slash")
            return

        # Intercept unknown commands for fuzzy suggestion before
        # delegating to cli._handle_slash_command — so we can track
        # the pending state and accept "yes" on the next input.
        from .commands import find_close_command, get_slash_commands

        known_first_words = {c.split()[0] for c in get_slash_commands()}
        if cmd not in known_first_words:
            suggestion = find_close_command(text)
            if suggestion:
                phrase = random.choice(_COMMAND_SUGGESTION_PHRASES).format(
                    cmd=suggestion,
                )
                self._append_output(f"\n  Unknown command: {cmd}. {phrase}\n\n")
                self._pending_command = suggestion
                self._set_suggestion("after_slash")
                return
            self._append_output(
                f"\n  Unknown command: {cmd}. Type /help for available commands.\n\n",
            )
            self._set_suggestion("after_slash")
            return

        # /update command — handled directly in TUI for restart support
        if cmd == "/update":
            self._handle_update_command(parts[1:] if len(parts) > 1 else [])
            return

        from .cli import _handle_slash_command
        from .user_commands import UserCommandPrompt

        result = _handle_slash_command(text, self._agent, self._store)
        if result == "exit":
            self._app.exit()
            return
        if isinstance(result, UserCommandPrompt):
            # User-defined command — render to a prompt and spawn an
            # agent turn (same path as a regular message).
            self._busy = True
            self._interrupted = False
            self._append_output(f"you> /{result.command_name}\n\n")
            t = threading.Thread(
                target=self._run_agent_turn,
                args=(result.prompt,),
                daemon=True,
            )
            t.start()
            return
        if result:
            clean = _strip_ansi(result)
            self._append_output(clean + "\n")
        self._set_suggestion("after_slash")

    # -- Update system -------------------------------------------------------

    def _on_update_available(self, info: VersionInfo) -> None:
        """Callback from BackgroundUpdateChecker (runs on background thread)."""
        if self._update_dismissed:
            return
        self._update_info = info
        if self._busy:
            # Stash for later — show after current turn completes
            self._pending_update_notification = info
        else:
            self._show_update_notification(info)

    def _show_update_notification(self, info: VersionInfo) -> None:
        """Inject an inline update notification into the output buffer."""
        self._append_output(
            f"\n  [system] Update available: {info.current} \u2192 {info.available}\n"
            f"  Type /update to install, or /update later to defer.\n\n"
        )

    def _handle_update_command(self, args: list[str]) -> None:
        """Handle /update [now|later|check]."""
        subcmd = args[0].lower() if args else "now"

        if subcmd == "later":
            self._update_dismissed = True
            self._pending_update_notification = None
            self._append_output("\n  Update deferred for this session.\n\n")
            self._set_suggestion("after_slash")
            return

        if subcmd == "check":
            self._append_output("\n  Checking for updates...\n")
            t = threading.Thread(
                target=self._check_update_manual,
                daemon=True,
            )
            t.start()
            return

        # "now" (default) — save session, apply update, restart
        self._perform_update_and_restart()

    def _check_update_manual(self) -> None:
        """Manually triggered version check (runs in background thread)."""
        try:
            from axiom.extensions.builtins.update.version_check import VersionChecker

            checker = VersionChecker()
            info = checker.check_remote_version(timeout=10.0)
            self._update_info = info
            if info.is_newer:
                self._show_update_notification(info)
            else:
                self._append_output(f"  Already up to date ({info.current}).\n\n")
        except Exception as e:
            self._append_output(f"  Could not check: {e}\n\n")

    def _perform_update_and_restart(self) -> None:
        """Save session, run update, and exec into new process."""
        # Save session immediately
        self._store.save(self._agent.session)
        session_id = self._agent.session.session_id

        self._append_output("\n  Saving session and updating...\n")

        # Run update in a thread so the TUI stays responsive briefly
        def _do_update():
            try:
                from axiom.extensions.builtins.update.cli import Updater

                updater = Updater()
                # This calls os.execv and does not return on success
                updater.update_and_restart(session_id, pull=True)
            except Exception as e:
                # If os.execv fails or update fails, show error
                self._append_output(f"\n  [error] Update failed: {e}\n\n")
                self._busy = False

        self._busy = True
        t = threading.Thread(target=_do_update, daemon=True)
        t.start()

    def _inject_restart_message(self) -> None:
        """After resuming from an update restart, show a friendly summary."""
        ctx = self._restart_ctx
        if not ctx:
            return

        old_v = ctx.get("old_version", "?")
        new_v = ctx.get("new_version", "?")

        lines = [
            "\u2500" * 42,
            f"  Updated {old_v} \u2192 {new_v}.",
            "",
        ]

        # Show changelog summary if available
        try:
            from axiom.extensions.builtins.update.version_check import (
                clear_pending_changelog,
                read_pending_changelog,
            )

            changelog = read_pending_changelog()
            if changelog:
                categories = changelog.get("categories", {})
                _labels = {
                    "features": "New",
                    "fixes": "Fixed",
                    "improvements": "Improved",
                }
                for key, _label in _labels.items():
                    items = categories.get(key, [])
                    for item in items[:3]:
                        lines.append(f"    - {item}")
                if any(categories.values()):
                    lines.append("")
                clear_pending_changelog()
        except Exception:
            pass

        lines.extend(
            [
                "  Your conversation is right where you left it.",
                "\u2500" * 42,
                "",
            ]
        )

        self._append_output("\n".join(lines) + "\n")
        self._restart_ctx = None  # Only show once

    def _check_affirmative(self, text: str) -> bool:
        """Check if text is an affirmative response to a pending command.

        If affirmative, echo the command and execute it.
        Returns True if the suggestion was accepted and executed.
        """
        cmd = self._pending_command
        if cmd is None:
            return False

        if text.lower().strip() in _AFFIRMATIVES:
            self._pending_command = None
            self._append_output(f"you> {cmd}\n\n")
            self._handle_slash_command(cmd)
            return True
        return False

    # -- Session picker ------------------------------------------------------

    def _open_picker(self, mode: PickerMode) -> None:
        """Open the interactive session picker overlay."""
        items = self._load_picker_items(include_archived=False)
        if not items:
            self._append_output("\n  No saved sessions.\n\n")
            return

        self._picker = PickerState(
            mode=mode,
            items=items,
            cursor=0,
            checked=set(),
            include_archived=False,
        )
        self._render_picker()

    def _load_picker_items(self, include_archived: bool) -> list[dict[str, Any]]:
        """Load session metadata for the picker."""
        session_ids = self._store.list_sessions(include_archived=include_archived)
        items: list[dict[str, Any]] = []
        for sid in session_ids[:30]:
            meta = self._store.load_meta(sid)
            if meta:
                items.append(meta)
        return items

    def _toggle_picker_archived(self) -> None:
        """Toggle the 'include archived' flag and reload picker items."""
        p = self._picker
        if p is None:
            return
        p.include_archived = not p.include_archived
        p.items = self._load_picker_items(include_archived=p.include_archived)
        p.cursor = 0
        p.checked.clear()
        self._render_picker()

    def _picker_float_content(self) -> FormattedText:
        """Build picker UI as FormattedText for the Float overlay."""
        p = self._picker
        if p is None:
            return FormattedText([])

        lines: list[str] = []
        archive_toggle = "[x]" if p.include_archived else "[ ]"
        archive_hint = f"  {archive_toggle} include archived (Tab to toggle)"

        if p.mode == PickerMode.MULTI:
            lines.append(
                "  Archive sessions"
                " (↑↓ navigate · Space toggle"
                " · Enter confirm · Esc cancel)"
            )
        else:
            lines.append(
                "  Select a session (↑↓ navigate · Enter to load · Esc cancel)"
            )
        lines.append(archive_hint)
        lines.append("")

        for i, item in enumerate(p.items):
            sid = item.get("id", "?")[:12]
            title = item.get("title") or "(untitled)"
            if len(title) > 30:
                title = title[:27] + "..."
            msg_count = item.get("message_count", 0)
            updated = time_ago(item.get("updated_at", ""))
            archived_tag = " (archived)" if item.get("archived") else ""
            pointer = " > " if i == p.cursor else "   "

            if p.mode == PickerMode.MULTI:
                check = "[x]" if i in p.checked else "[ ]"
                lines.append(
                    f"{pointer}{check} {sid}  {title:<30s}"
                    f"  {msg_count:>3d} msgs  {updated}{archived_tag}"
                )
            elif item.get("id") == "__new__":
                lines.append(f"{pointer}+  {'New session':<30s}")
            else:
                lines.append(
                    f"{pointer}{sid}  {title:<30s}  {msg_count:>3d} msgs  {updated}{archived_tag}"
                )

        lines.append("")
        if p.items:
            item = p.items[p.cursor]
            if item.get("id") == "__new__":
                lines.append("  └ Start a fresh conversation")
            else:
                full_title = item.get("title") or "(untitled)"
                lines.append(f"  └ {full_title}")
        lines.append("")

        return FormattedText([("class:picker", "\n".join(lines))])

    def _render_picker(self) -> None:
        """Invalidate the app so the picker Float re-renders from current state."""
        self._app.invalidate()

    def _render_session_history(self) -> None:
        """Render the loaded session's messages into the output buffer."""
        session = self._agent.session
        if not session.messages:
            return
        title = session.title or "(untitled)"
        header = f"  Session {session.session_id[:12]} — {title}\n\n"
        self._append_output(header)
        for msg in session.messages:
            if msg.role == "user":
                self._append_output(f"you> {msg.content}\n\n")
            elif msg.role == "assistant" and msg.content:
                self._append_output(msg.content + "\n\n")

    def _confirm_picker(self) -> None:
        """Handle Enter in the picker — resume or archive selected sessions."""
        p = self._picker
        if p is None:
            return

        from .commands import cmd_resume

        if p.mode == PickerMode.SELECT:
            item = p.items[p.cursor]
            sid = item["id"]

            # "New session" sentinel — just dismiss and stay on blank session
            if sid == "__new__":
                self._dismiss_picker()
                self._set_suggestion("welcome")
                return

            # Close picker and clear output for the new session
            self._picker = None
            with self._output_lock:
                self._raw_output = ""
                self._output_buffer.set_document(
                    Document("", 0),
                    bypass_readonly=True,
                )
            result = cmd_resume(sid, self._store, self._agent)
            clean = _strip_ansi(result)
            self._append_output(clean + "\n")
            self._render_session_history()
        elif p.mode == PickerMode.MULTI:
            checked = sorted(p.checked)
            if not checked:
                # Nothing selected — dismiss silently
                self._dismiss_picker()
                return
            sids = [p.items[i]["id"] for i in checked]
            self._dismiss_picker()
            archived = []
            for sid in sids:
                if self._store.archive(sid):
                    archived.append(sid)
            if archived:
                n = len(archived)
                summary = f"  Archived {n} session{'s' if n != 1 else ''}."
                summary += "\n  Archived sessions are kept in sessions/archive/"
                summary += " and can be restored with /resume <id>.\n"
                self._append_output(summary)

        self._set_suggestion("after_slash")

    def _dismiss_picker(self) -> None:
        """Close picker and restore output buffer from canonical _raw_output."""
        if self._picker is None:
            return
        self._picker = None
        with self._output_lock:
            self._output_buffer.set_document(
                Document(self._raw_output, len(self._raw_output)),
                bypass_readonly=True,
            )
        self._app.invalidate()

    # -- Agent turn (background thread) --------------------------------------

    def _run_agent_turn(self, text: str) -> None:
        from .agent import ChatTurnCancelled

        # Clear thinking buffer from previous turn (T2.6)
        self._thinking_buffer = ""

        try:
            self._start_spinner("Thinking")

            if self._stream and self._agent.gateway.available:
                self._agent.turn(text, stream=True)
            else:
                self._agent.turn(text, stream=False)

            self._stop_spinner()
            self._append_output("\n")

            # Update persistent status bar (not output)
            if self._agent.gateway.active_provider:
                self._last_model = self._agent.gateway.active_provider.model
            usage = self._agent.usage
            if usage.turns:
                last = usage.turns[-1]
                self._last_tokens = f"{last.input_tokens}in/{last.output_tokens}out"
                if last.cost > 0:
                    self._last_cost = f"${last.cost:.4f}"

            self._store.save(self._agent.session)
            self._set_suggestion("after_turn")
        except ChatTurnCancelled:
            self._stop_spinner()
            self._append_output("\n  [cancelled]\n\n")
            self._set_suggestion("after_turn")
        except Exception as e:
            self._stop_spinner()
            self._append_output(f"\n  [error] {e}\n\n")
            self._set_suggestion("after_error")
        finally:
            self._agent.reset_cancel()
            self._busy = False
            # Reset inflight token counters for next turn (T2.5)
            self._inflight_tokens_in = 0
            self._inflight_tokens_out = 0
            self._inflight_started_at = None
            # Show deferred update notification if one arrived mid-turn
            if self._pending_update_notification and not self._update_dismissed:
                self._show_update_notification(self._pending_update_notification)
                self._pending_update_notification = None
            self._app.invalidate()

    # -- Spinner -------------------------------------------------------------

    def _start_spinner(self, label: str = "Thinking") -> None:
        if self._spinner_visible:
            return  # idempotent — already running
        self._spinner_label = label
        self._spinner_sub_state = ""
        self._spinner_input_tokens = 0
        self._spinner_output_tokens = 0
        self._spinner_start = time.monotonic()
        self._spinner_stop_event.clear()
        self._spinner_visible = True

        self._spinner_thread = threading.Thread(
            target=self._spinner_loop,
            daemon=True,
        )
        self._spinner_thread.start()

    def _stop_spinner(self) -> None:
        self._spinner_stop_event.set()
        # Set visible=False and clear text BEFORE join so no stale glyph
        # survives the final 60ms wait in _spinner_loop.
        self._spinner_visible = False
        self._spinner_text = FormattedText([])
        if self._spinner_thread is not None:
            self._spinner_thread.join(timeout=1.0)
            self._spinner_thread = None
        self._app.invalidate()

    def _spinner_loop(self) -> None:
        idx = 0
        while not self._spinner_stop_event.is_set():
            frame = PULSE_FRAMES[idx % len(PULSE_FRAMES)]
            elapsed = time.monotonic() - self._spinner_start

            label = self._spinner_label
            sub = self._spinner_sub_state
            in_tok = self._spinner_input_tokens
            out_tok = self._spinner_output_tokens

            # Build FormattedText with 24-bit color
            color = f"fg:#{frame.r:02x}{frame.g:02x}{frame.b:02x}"
            parts: list[tuple[str, str]] = [
                ("", "  "),
                (color, frame.symbol),
                ("", f" {label}\u2026 "),
            ]

            # Detail parts
            detail_items = [_format_elapsed(elapsed)]
            total_tok = in_tok + out_tok
            if total_tok > 0:
                detail_items.append(
                    f"\u2193 {_format_tokens(total_tok)} tokens",
                )
            if sub:
                detail_items.append(sub)
            detail_items.append("esc to interrupt")
            detail = " \u00b7 ".join(detail_items)
            parts.append(("class:spinner.detail", f"({detail})"))

            self._spinner_text = FormattedText(parts)
            self._app.invalidate()

            idx += 1
            self._spinner_stop_event.wait(_FRAME_INTERVAL)

    # -- Approval handling ---------------------------------------------------

    def _handle_approval_input(self, text: str) -> None:
        with self._output_lock:
            req = self._approval_pending
        if req is None:
            return

        raw = text.strip()
        if raw == "A":
            req.choice = "A"
        elif raw == "D":
            req.choice = "D"
        elif raw.lower() in ("a", "approve"):
            req.choice = "a"
        elif raw.lower() in ("r", "reject", "s", "skip"):
            req.choice = "r"
        else:
            self._append_output(
                "  Choose [a]pprove, [A]lways allow, [r]eject, [D]eny always, or [s]kip\n",
            )
            return

        with self._output_lock:
            self._approval_pending = None
        self._set_suggestion("after_approval")
        req.event.set()

    # -- Run -----------------------------------------------------------------

    def run(self) -> None:
        """Start the full-screen TUI."""
        # Wire our TUI render provider into the agent
        provider = _TuiRenderProvider(self)
        self._agent.set_render_provider(provider)

        # Initialize status bar with gateway model before first turn
        gw = self._agent.gateway
        if gw and gw.active_provider:
            self._last_model = gw.active_provider.model

        # Show welcome
        workspace_ctx = getattr(self._agent, "_workspace_context", "")
        provider.render_welcome(
            gateway=self._agent.gateway,
            show_banner=self._show_banner,
            workspace_context=workspace_ctx,
        )

        # If resuming from an update restart, inject the friendly message
        if self._restart_ctx:
            self._inject_restart_message()

        # Auto-open session picker if there are saved sessions to choose from
        if self._auto_picker:
            self._auto_picker = False
            items = self._load_picker_items(include_archived=False)
            if items:
                new_item = {
                    "id": "__new__",
                    "title": "New session",
                    "message_count": 0,
                    "updated_at": "",
                }
                self._picker = PickerState(
                    mode=PickerMode.SELECT,
                    items=[new_item] + items,
                    cursor=min(1, len(items)),  # preselect latest session
                    checked=set(),
                )
                self._render_picker()

        # Start background update checker
        self._update_checker = BackgroundUpdateChecker(
            on_update_available=self._on_update_available,
        )
        self._update_checker.start()

        # Suppress direnv noise when the fullscreen app exits and the
        # terminal restores (direnv re-evaluates .envrc on every cd/exec).
        old_log = os.environ.get("DIRENV_LOG_FORMAT")
        os.environ["DIRENV_LOG_FORMAT"] = ""
        try:
            # Clear terminal so shell scrollback doesn't show behind the TUI
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
            self._app.run()
        finally:
            if self._update_checker:
                self._update_checker.stop()
            if old_log is None:
                os.environ.pop("DIRENV_LOG_FORMAT", None)
            else:
                os.environ["DIRENV_LOG_FORMAT"] = old_log
            # Mermaid scratch cleanup handled by TIDY on process exit


# ---------------------------------------------------------------------------
# TUI Render Provider
# ---------------------------------------------------------------------------


class _TuiRenderProvider(RenderProvider):
    """Render provider that writes to the FullScreenChat output buffer.

    All output is plain text — the _OutputLexer handles styling at
    render time based on markdown patterns.
    """

    def __init__(self, tui: FullScreenChat):
        self._tui = tui
        self._current_tool_input: str = ""

    def stream_text(self, chunks: Iterator[StreamChunk]) -> str:
        accumulated = ""
        first_text = True

        for chunk in chunks:
            if chunk.type == "text":
                if first_text:
                    self._tui._stop_spinner()
                    first_text = False
                    # Record when streaming started for live status line (T2.5)
                    if self._tui._inflight_started_at is None:
                        import time as _time

                        self._tui._inflight_started_at = _time.monotonic()
                accumulated += chunk.text
                self._tui._append_output(chunk.text)

            elif chunk.type == "thinking_delta":
                # T2.6: accumulate in separate buffer, not main output
                self._tui._thinking_buffer += chunk.text

            elif chunk.type == "usage":
                # T2.5: track inflight tokens for live status line
                self._tui._inflight_tokens_in += chunk.input_tokens
                self._tui._inflight_tokens_out += chunk.output_tokens
                self._tui._spinner_input_tokens += chunk.input_tokens
                self._tui._spinner_output_tokens += chunk.output_tokens

            elif chunk.type == "tool_use_start":
                # T2.3: show tool header in output before approval prompt
                self._tui._stop_spinner()
                self._tui._append_output(f"\n┌ [tool] {chunk.tool_name}\n")
                self._current_tool_input = ""

            elif chunk.type == "tool_input_delta":
                # T2.3: stream tool input assembly to output buffer (dim)
                if chunk.tool_input_json:
                    self._current_tool_input += chunk.tool_input_json
                    self._tui._append_output(chunk.tool_input_json)

            elif chunk.type == "tool_use_end":
                # T2.3: close the box-drawing frame
                self._tui._append_output("\n└\n")
                self._current_tool_input = ""

            elif chunk.type == "done":
                self._tui._stop_spinner()
                if accumulated and not accumulated.endswith("\n"):
                    self._tui._append_output("\n")
                # Render any mermaid blocks to SVG and replace with placeholders
                self._tui._process_mermaid()
                # Render any ```math blocks via Sci Displays and replace
                # with placeholders (mirrors the Mermaid pattern).
                self._tui._process_math()
                # Re-wrap the full buffer now that streaming is complete —
                # partial lines accumulated during streaming only had
                # character-level wrapping from the Window.
                self._tui._rewrap_buffer()
                break

        return accumulated

    def render_welcome(
        self,
        gateway: Any = None,
        show_banner: bool = False,
        workspace_context: str = "",
    ) -> None:
        from importlib.metadata import version as pkg_version
        from pathlib import Path

        # NOTE: prompt-toolkit's renderer treats `│` and `|` as column
        # separators, so the multi-row AXI ASCII art (used by the
        # `axi --help` Rich banner) is shredded into detached vertical
        # bars when embedded here.  Use a tiny, vertical-bar-free
        # micro-mascot instead — eye binoculars + label, two lines.
        mascot = "  (◉)─(◉)\n    AXI\n"

        # Version
        try:
            from axiom.infra.branding import get_branding

            ver = pkg_version(get_branding().package_name)
        except Exception:
            ver = "dev"

        # Project path
        cwd = Path.cwd()

        lines = [mascot]
        # Brand-neutral banner line — a consumer layer overlays its own
        # branding via get_branding() when installed; vanilla Axiom
        # renders as the platform.
        try:
            _b = get_branding()
            _prod = _b.product_name
            _cli = _b.cli_name
        except Exception:
            _prod, _cli = "Axiom", "axi"
        lines.append(f"  {_cli} v{ver} — {_prod} · AXI\n")
        lines.append(f"  {cwd}\n")

        # Workspace context from model.yaml
        if workspace_context:
            from .workspace import workspace_summary_line

            summary = workspace_summary_line(workspace_context)
            if summary:
                lines.append(f"  {summary}\n")

        lines.append("\n  Type /help for commands, ctrl+d to exit.\n\n")

        self._tui._append_output("".join(lines))

    def render_tool_start(self, name: str, params: dict[str, Any]) -> None:
        self._tui._start_spinner(f"Running {name}")

    def render_tool_result(
        self,
        name: str,
        result: dict[str, Any],
        elapsed: float,
    ) -> None:
        self._tui._stop_spinner()
        if "error" in result:
            self._tui._append_output(
                f"  x {name} failed ({elapsed:.1f}s): {result['error']}\n",
            )
        else:
            self._tui._append_output(f"  v {name} ({elapsed:.1f}s)\n")

    def render_approval_prompt(self, action: Action) -> str:
        verb = getattr(action.category, "value", str(action.category)).lower()
        title = f"Approve {verb}"
        fill = max(0, 30 - len(title) - 5)
        top = f"┌─ {title} {'─' * fill}┐"
        bot = f"└{'─' * 28}┘"
        text = (
            f"\n  {top}\n"
            f"  │ {action.name}: {_format_params(action.params)}\n"
            f"  {bot}\n"
            f"  [a]pprove  [A]lways allow  [r]eject  [D]eny always  [s]kip\n\n"
        )
        self._tui._append_output(text)

        req = _ApprovalRequest(action=action)
        with self._tui._output_lock:  # T2.7 — protect _approval_pending write
            self._tui._approval_pending = req
        self._tui._app.invalidate()

        # Block agent thread until user responds (outside the lock to avoid deadlock)
        req.event.wait()
        return req.choice

    def render_action_result(self, action: Action) -> None:
        from axiom.infra.orchestrator.actions import ActionStatus

        if action.status == ActionStatus.COMPLETED:
            result = action.result or {}
            if "error" in result:
                self._tui._append_output(f"  x {result['error']}\n")
            else:
                for k, v in result.items():
                    self._tui._append_output(f"  {k}: {v}\n")
        elif action.status == ActionStatus.REJECTED:
            self._tui._append_output(f"  [skipped] {action.name}\n")
        elif action.status == ActionStatus.FAILED:
            self._tui._append_output(f"  [failed] {action.error}\n")

    def render_status(
        self,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost: float,
    ) -> None:
        # Status handled in _run_agent_turn; no-op to avoid double-print
        pass

    def render_thinking(self, text: str, collapsed: bool = True) -> None:
        if not text:
            return
        lines = text.splitlines()
        if collapsed and len(lines) > 3:
            display = lines[:3] + [f"... ({len(lines) - 3} more lines)"]
        else:
            display = lines
        for line in display:
            self._tui._append_output(f"  [thinking] {line}\n")

    def render_message(self, role: str, content: str) -> None:
        if role == "assistant" and content:
            self._tui._append_output(f"{content}\n")
        elif role == "system" and content:
            self._tui._append_output(f"  [system] {content}\n")

    def render_session_list(self, sessions: list[dict[str, Any]]) -> None:
        if not sessions:
            self._tui._append_output("  No saved sessions.\n")
            return
        self._tui._append_output("\n  Saved sessions:\n")
        for s in sessions:
            sid = s.get("id", "?")
            msgs = s.get("messages", 0)
            updated = s.get("updated", "")
            self._tui._append_output(
                f"  {sid}  {msgs} messages  {updated}\n",
            )
        self._tui._append_output("\n")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_params(params: dict[str, Any]) -> str:
    if not params:
        return "(no parameters)"
    return "  |  ".join(f"{k}={v}" for k, v in params.items())
