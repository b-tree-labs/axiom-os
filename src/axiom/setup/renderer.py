# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Jargon-free terminal output for axi config.

Centralizes all user-facing text so technical terms never leak to the user.
Uses ANSI escape codes with automatic fallback when color is unsupported.
"""
# pylint: disable=import-outside-toplevel,broad-exception-caught,redefined-outer-name,global-statement

from __future__ import annotations

import getpass
import os
import sys
import time

from axiom.infra.branding import get_branding as _get_branding

# ---------------------------------------------------------------------------
# Jargon map — single source of truth for user-facing terminology
# ---------------------------------------------------------------------------

JARGON_MAP: dict[str, str] = {
    # Environment variable names → display names
    "GITLAB_TOKEN": "GitLab access key",
    "GITHUB_TOKEN": "GitHub access key",
    "MS_GRAPH_CLIENT_ID": "Microsoft 365 app ID",
    "MS_GRAPH_CLIENT_SECRET": "Microsoft 365 app secret",
    "MS_GRAPH_TENANT_ID": "Microsoft 365 tenant ID",
    "ANTHROPIC_API_KEY": "Anthropic access key",
    "OPENAI_API_KEY": "OpenAI access key",
    "LINEAR_API_KEY": "Linear access key",
    # Generic technical terms → plain language
    "API key": "access key",
    "api key": "access key",
    "environment variable": "connection setting",
    "env var": "connection setting",
    "token": "access key",
    "OAuth": "secure login",
    "oauth": "secure login",
    "MS Graph API": "Microsoft 365 connection",
    "ms graph api": "Microsoft 365 connection",
    "CLI": "command-line tool",
    "endpoint": "connection address",
    "authentication": "sign-in",
    "credentials": "connection settings",
}


def friendly_name(technical_name: str) -> str:
    """Convert a technical name to its user-friendly equivalent."""
    return JARGON_MAP.get(technical_name, technical_name)


# ---------------------------------------------------------------------------
# Color support detection
# ---------------------------------------------------------------------------

def _supports_color() -> bool:
    """Detect whether the terminal supports ANSI color codes."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    term = os.environ.get("TERM", "")
    if term == "dumb":
        return False
    return True


_COLOR_ENABLED = _supports_color()


def _use_color() -> bool:
    """Return whether color output is currently enabled."""
    return _COLOR_ENABLED


def set_color_enabled(enabled: bool) -> None:
    """Override color detection (useful for testing)."""
    global _COLOR_ENABLED
    _COLOR_ENABLED = enabled


# ---------------------------------------------------------------------------
# ANSI codes
# ---------------------------------------------------------------------------

class _Colors:  # pylint: disable=too-few-public-methods
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    CYAN = "\033[36m"
    BLUE = "\033[34m"
    BRIGHT_BLUE = "\033[94m"
    MAGENTA = "\033[35m"
    # Accent blue — the brand color for highlights
    ACCENT_BLUE = "\033[38;2;0;207;255m"


def _c(code: str, text: str) -> str:
    """Wrap text in ANSI codes if color is enabled."""
    if _use_color():
        return f"{code}{text}{_Colors.RESET}"
    return text


# ---------------------------------------------------------------------------
# Display primitives
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Axi mascot (Axiom default)
#
# Axi is the platform's default orchestrator voice when no domain
# agent is registered.
#
# Interactive hover zones (character positions, 0-indexed from art top-left):
#   EYES:    row 3, cols 13 and 30  (◕ sensors)
#   LIGHTS:  row 1, cols 10 and 33  (◈ nav lights)
#   HULL:    rows 0-5, cols 8-39    (ship body)
#   ENGINES: row 7, cols 11/16/21/26/31  (▼ nozzles)
# ---------------------------------------------------------------------------

# 2026-05-04 — replaced the legacy rocket mascot with the
# AXI character.  Same character now shipped from `axi --help`,
# `axi chat`, and the setup wizard so the brand-face is consistent
# across surfaces.  See `axiom_cli._print_welcome_banner` for the
# colored Rich-rendered version; this is the plain ANSI variant
# used by the FullScreen TUI's mascot pane.
# Single-line box-drawing only (no `═║╔╗╚╝`) and narrow `●` pupil so
# every char is reliably single-width.  Earlier double-line versions
# rendered with broken alignment in some terminals (chat-TUI screenshot
# 2026-05-04: right cylinder + body right-side cropped because mixed
# `─` / `═` widths drifted columns).  The `axi --help` Rich-rendered
# variant in `axiom_cli._print_welcome_banner` keeps the prettier
# double-line frame because Rich knows the width.
# Iteration 7 — pure ASCII art for the chat TUI mascot pane.  The
# chat-TUI renderer auto-treats Unicode box-drawing chars (`┌─┐│└┘`)
# as UI frame borders, which fragments the multi-line art into tiny
# detached boxes.  Plain ASCII (`+-|+-+`) renders as text so the art
# stays intact.  The `axi --help` Rich-rendered banner uses prettier
# Unicode chars because Rich knows widths and won't reframe.
_AXI_ART_LINES = [
    "   +----+ +----+   ",  # 0: eye top frame (pupil contained)
    "   | (o)|-|(o) |   ",  # 1: pupil row + sides + bridge
    "   +----+ +----+   ",  # 2: eye bottom frame
    "         |         ",  # 3: neck row 1
    "         |         ",  # 4: neck row 2
    "   +-----+-----+   ",  # 5: body top (13ch wide)
    "   |           |   ",  # 6: top air row
    "   | <       > |   ",  # 7: hands (claws inward)
    "   | |       | |   ",  # 8: forearm verticals
    "   | L       J |   ",  # 9: elbow corners
    "   |    AXI    |   ",  # 10: label placard (low on body)
    "   +=+=+   +=+=+   ",  # 11: body→tread integration (tucked up)
    "   +=+=+   +=+=+   ",  # 12: tread bottoms
]

# Interactive zone positions in the AXI art.
#   * eyes — the bullseye pupils
#   * lights — the binocular bridge (small accent)
#   * engines — center of each track (one per foot)
_AXI_ZONES: dict[str, list[tuple[int, int]]] = {
    "eyes":    [(1, 6), (1, 12)],
    "lights":  [(1, 9)],          # bridge `-` between eye housings
    "engines": [(12, 5), (12, 13)],  # one wheel per foot
}

_AXI_ZONE_LABELS = {
    "eyes":    "Visual Sensors",
    "lights":  "Optical Bridge",
    "engines": "Treads",
}

_AXI_ZONE_COLORS = {
    "eyes":    "\033[38;2;0;188;212m",    # cyan-glow pupils
    "lights":  "\033[38;2;255;193;7m",    # bridge accent (warm yellow)
    "engines": "\033[38;2;120;120;120m",  # dim weathered rubber
    "hull":    "\033[38;2;255;193;7m",    # AXI hull yellow
}

_AXI_CHARS_NORMAL = {"eyes": "◉", "lights": "═", "engines": "o"}
_AXI_CHARS_HOT    = {"eyes": "◎", "lights": "✦", "engines": "O"}


def _render_axi_art(hot_zone: str | None = None) -> list[str]:
    """Render Axi art lines with optional zone highlighted."""
    if not _use_color():
        return list(_AXI_ART_LINES)

    dim   = _Colors.DIM
    reset = _Colors.RESET
    hull_c = _AXI_ZONE_COLORS["hull"]

    lines = list(_AXI_ART_LINES)

    # Colour non-highlighted zones dimly
    def zone_color(zone: str) -> str:
        if zone == hot_zone:
            return _AXI_ZONE_COLORS[zone]
        return dim

    # Replace zone characters with colored versions
    for zone, positions in _AXI_ZONES.items():
        char = _AXI_CHARS_HOT[zone] if zone == hot_zone else _AXI_CHARS_NORMAL[zone]
        color = zone_color(zone)
        for row, col in positions:
            line = lines[row]
            lines[row] = line[:col] + color + char + reset + line[col + 1:]

    # Color hull lines.  Row 11 (tread housing tops) stays hull yellow.
    # Row 12 (tread bottoms with cleats) is left dim/un-hull-colored —
    # that's where the engines zone glyphs live (worn-rubber tread).
    result = []
    for i, line in enumerate(lines):
        if i == 12:
            result.append(line)  # engines (tread cleats) handled above
        else:
            result.append(f"{hull_c}{line}{reset}")
    return result


def axi_banner_lines() -> list[str]:
    """Return the canonical Axi mascot art lines (plain, no ANSI)."""
    return list(_AXI_ART_LINES)


def _supports_mouse() -> bool:
    """Return True if terminal likely supports xterm mouse tracking."""
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        return False
    term = os.environ.get("TERM", "")
    colorterm = os.environ.get("COLORTERM", "")
    return "xterm" in term or "256color" in term or colorterm in ("truecolor", "24bit")


def _get_cursor_pos() -> tuple[int, int] | None:
    """Query terminal for current cursor position. Returns (row, col) or None."""
    import select
    import termios
    import tty
    try:
        old = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())
        sys.stdout.write("\033[6n")
        sys.stdout.flush()
        buf = ""
        while True:
            r, _, _ = select.select([sys.stdin], [], [], 0.2)
            if not r:
                break
            ch = sys.stdin.read(1)
            buf += ch
            if ch == "R":
                break
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
        # Parse ESC[row;colR
        if buf.startswith("\033[") and buf.endswith("R"):
            parts = buf[2:-1].split(";")
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return None


# pylint: disable=too-many-locals,too-many-branches,too-many-statements,too-many-nested-blocks
def axi_banner(interactive: bool | None = None) -> None:
    """Print the Axi mascot banner.

    If *interactive* is True (or None and mouse is supported), enables
    xterm mouse tracking so hovering over zones highlights them and shows
    a label. Exits after any keypress or 6 seconds.
    """
    if interactive is None:
        interactive = _supports_mouse()

    if not interactive or not _use_color():
        _print_axi_static()
        return

    _print_axi_interactive()


def _print_axi_static() -> None:
    """Print Axi art with a brief pulse on the eyes then settle."""
    art_start = 2  # blank lines before art

    print("\n" * art_start, end="")
    lines = _render_axi_art()
    for line in lines:
        print(f"  {line}")
    print()

    if not _use_color():
        return

    # Brief glow pulse: eyes flash twice then settle
    sys.stdout.flush()
    for _ in range(2):
        time.sleep(0.18)
        # Use ANSI cursor up to re-render just the eye line
        up = art_start + len(_AXI_ART_LINES) - 3  # rows to go up to eye line
        sys.stdout.write(f"\033[{up}A\r")
        hot_lines = _render_axi_art("eyes")
        sys.stdout.write(f"  {hot_lines[3]}\n")
        sys.stdout.write(f"\033[{up - 1}B\r")
        sys.stdout.flush()
        time.sleep(0.18)
        sys.stdout.write(f"\033[{up}A\r")
        normal_lines = _render_axi_art()
        sys.stdout.write(f"  {normal_lines[3]}\n")
        sys.stdout.write(f"\033[{up - 1}B\r")
        sys.stdout.flush()


def _print_axi_interactive() -> None:
    """Interactive Axi banner with mouse hover zone highlighting."""
    import select
    import termios
    import tty

    # Print art and record where it started on screen
    print()
    art_start = _get_cursor_pos()
    lines = _render_axi_art()
    for line in lines:
        print(f"  {line}")
    print()
    # Print instructions
    print(_c(_Colors.DIM, "  hover to explore · any key to continue"))
    print()
    sys.stdout.flush()

    if art_start is None:
        return  # Couldn't get cursor position, skip interaction

    art_row0, art_col0 = art_start
    art_col0 += 2  # account for the "  " prefix

    # Enable mouse tracking (any-event + SGR extended coords)
    sys.stdout.write("\033[?1003h\033[?1006h")
    sys.stdout.flush()

    old_settings = None
    try:
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        tty.setraw(fd)

        current_zone: str | None = None
        deadline = time.time() + 6.0

        while time.time() < deadline:
            r, _, _ = select.select([sys.stdin], [], [], 0.05)
            if not r:
                continue

            buf = sys.stdin.read(1)
            if buf == "\033":
                # Read more of the escape sequence
                r2, _, _ = select.select([sys.stdin], [], [], 0.02)
                if r2:
                    buf += sys.stdin.read(20)
            else:
                # Any non-escape key → exit
                break

            # Parse SGR mouse event: ESC[<btn;col;rowM (move)
            if buf.startswith("\033[<") and buf.endswith("M"):
                inner = buf[3:-1]
                parts = inner.split(";")
                if len(parts) == 3:
                    try:
                        btn, mcol, mrow = int(parts[0]), int(parts[1]), int(parts[2])
                    except ValueError:
                        continue

                    if btn != 35:  # 35 = mouse move with no button
                        continue

                    # Convert to art-relative position
                    art_r = mrow - art_row0
                    art_c = mcol - art_col0

                    new_zone: str | None = None
                    if 0 <= art_r < len(_AXI_ART_LINES):
                        for zone, positions in _AXI_ZONES.items():
                            for row, col in positions:
                                if art_r == row and abs(art_c - col) <= 1:
                                    new_zone = zone
                                    break
                        if new_zone is None and 0 <= art_r <= 6:
                            new_zone = "hull"

                    if new_zone != current_zone:
                        current_zone = new_zone
                        # Redraw art in place
                        n_lines = len(_AXI_ART_LINES) + 3  # art + blank + hint + blank
                        sys.stdout.write(f"\033[{n_lines}A")  # move up
                        sys.stdout.write("\r")
                        highlight = current_zone if current_zone != "hull" else None
                        hot_lines = _render_axi_art(highlight)
                        for line in hot_lines:
                            sys.stdout.write(f"  {line}\033[K\n")
                        sys.stdout.write("\n")
                        # Update hint line
                        label = _AXI_ZONE_LABELS.get(current_zone or "", "")
                        if label:
                            hint = f"  ✦ {label}"
                            color = _AXI_ZONE_COLORS.get(current_zone or "hull", "")
                            sys.stdout.write(f"{color}{hint}{_Colors.RESET}\033[K\n")
                        else:
                            sys.stdout.write(
                                _c(_Colors.DIM, "  hover to explore · any key to continue")
                                + "\033[K\n"
                            )
                        sys.stdout.write("\n")
                        sys.stdout.flush()

    except Exception:
        pass
    finally:
        # Disable mouse tracking, restore terminal
        sys.stdout.write("\033[?1003l\033[?1006l")
        sys.stdout.flush()
        if old_settings is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
            except Exception:
                pass


def banner() -> None:
    """Print the active mascot banner (reads branding registry).

    Axi (default) or the domain product's custom banner_fn if registered.
    Domain products with no banner_fn get a simple styled product name header.
    """
    try:
        b = _get_branding()
        if b.banner_fn is not None:
            b.banner_fn()
            return
        if b.cli_name != "axi":
            # Domain product registered but no custom banner — print product name
            name_spaced = "  ".join(b.product_name.upper())
            print()
            print(_c(_Colors.BOLD + _Colors.ACCENT_BLUE, f"  {name_spaced}"))
            print(_c(_Colors.DIM, f"  {b.tagline}"))
            print()
            return
    except ImportError:
        pass
    # Default: Axi
    axi_banner()


def heading(text: str) -> None:
    """Print a section heading."""
    print()
    print(_c(_Colors.BOLD + _Colors.ACCENT_BLUE, f"  {text}"))
    print(_c(_Colors.DIM, "  " + "─" * len(text)))


def status_line(label: str, value: str, ok: bool) -> None:
    """Print a status line with a check/cross indicator."""
    icon = _c(_Colors.GREEN, "✓") if ok else _c(_Colors.RED, "✗")
    print(f"  {icon} {label}: {value}")


def progress_bar(current: int, total: int, width: int = 30) -> None:
    """Print a simple progress bar."""
    if total == 0:
        return
    filled = int(width * current / total)
    progress_bar_char = "█" * filled + "░" * (width - filled)
    pct = int(100 * current / total)
    line = f"  [{progress_bar_char}] {pct}% ({current}/{total})"
    print(_c(_Colors.CYAN, line), end="\r" if current < total else "\n")


def divider() -> None:
    """Print a horizontal divider."""
    print(_c(_Colors.DIM, "  " + "─" * 50))


def info(text: str) -> None:
    """Print an informational message."""
    print(f"  {_c(_Colors.ACCENT_BLUE, 'ℹ')} {text}")


def success(text: str) -> None:
    """Print a success message."""
    print(f"  {_c(_Colors.GREEN, '✓')} {text}")


def warning(text: str) -> None:
    """Print a warning message."""
    print(f"  {_c(_Colors.YELLOW, '⚠')} {text}")


def error(text: str) -> None:
    """Print an error message."""
    print(f"  {_c(_Colors.RED, '✗')} {text}")


def blank() -> None:
    """Print a blank line."""
    print()


def text(msg: str) -> None:
    """Print plain body text."""
    print(f"  {msg}")


def numbered_steps(steps: list[str]) -> None:
    """Print a numbered list of steps."""
    for i, step in enumerate(steps, 1):
        print(f"  {_c(_Colors.BOLD, str(i) + '.')} {step}")


# ---------------------------------------------------------------------------
# Input primitives
# ---------------------------------------------------------------------------

def prompt_choice(question: str, options: list[str]) -> int:
    """Prompt the user to choose from numbered options. Returns 0-based index.

    Raises KeyboardInterrupt on Ctrl+C so the wizard can exit cleanly.
    """
    print()
    text(question)
    for i, opt in enumerate(options, 1):
        print(f"    {_c(_Colors.BOLD, str(i))} — {opt}")
    while True:
        try:
            raw = input(_c(_Colors.CYAN, "  → ")).strip()
        except EOFError:
            print()
            return 0
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return idx
        warning(f"Please enter a number from 1 to {len(options)}")


def prompt_yn(question: str, default: bool = True) -> bool:
    """Prompt yes/no. Returns boolean.

    Raises KeyboardInterrupt on Ctrl+C so the wizard can exit cleanly.
    """
    hint = "Y/n" if default else "y/N"
    try:
        raw = input(f"  {question} [{hint}]: ").strip().lower()
    except EOFError:
        print()
        return default
    if raw in ("y", "yes"):
        return True
    if raw in ("n", "no"):
        return False
    return default


def prompt_secret(label: str) -> str:
    """Prompt for a secret value (no echo).

    Raises KeyboardInterrupt on Ctrl+C so the wizard can exit cleanly.
    """
    try:
        return getpass.getpass(f"  {label}: ")
    except EOFError:
        print()
        return ""


def prompt_text(label: str, default: str = "") -> str:
    """Prompt for text input with optional default.

    Raises KeyboardInterrupt on Ctrl+C so the wizard can exit cleanly.
    """
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"  {label}{suffix}: ").strip()
    except EOFError:
        print()
        return default
    return raw or default


# ---------------------------------------------------------------------------
