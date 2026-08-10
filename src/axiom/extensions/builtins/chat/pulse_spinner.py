# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Pulse animation spinner with live status.

A calming thinking indicator: a small dot pulses from dark blue
to light blue and back, creating a gentle breathing effect.

Status line format:
    ● Thinking… (15s · ↓ 1.2k tokens · streaming)
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass

from axiom.setup.renderer import _use_color

# ---------------------------------------------------------------------------
# Frame definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PulseFrame:
    """One animation frame: symbol, 24-bit RGB colour, bold flag."""

    symbol: str
    r: int
    g: int
    b: int
    bold: bool = False


# 10 frames, ~600 ms cycle at 60 ms/frame.
# Ramps from Cherenkov shadow (0,80,110) → Cherenkov peak (0,207,255) → back.
# Phase-locked: '•' glyph appears only at frames 4-5 where brightness peaks.
# Optional shape glyph set ◌→◍→◉→◍→◌ gives a secondary shape pulse.
PULSE_FRAMES: tuple[PulseFrame, ...] = (
    PulseFrame("◌", 0, 80, 110),   # 0 Cherenkov shadow
    PulseFrame("◌", 0, 110, 150),  # 1 rising
    PulseFrame("◍", 0, 145, 195),  # 2 building
    PulseFrame("◍", 0, 175, 230),  # 3 near peak
    PulseFrame("◉", 0, 207, 255),  # 4 Cherenkov peak (phase-locked)
    PulseFrame("•", 0, 207, 255),  # 5 peak hold   (phase-locked)
    PulseFrame("◍", 0, 170, 225),  # 6 fading
    PulseFrame("◍", 0, 135, 185),  # 7 dimming
    PulseFrame("◌", 0, 100, 140),  # 8 afterglow
    PulseFrame("◌", 0, 80, 110),   # 9 rest
)

_FRAME_INTERVAL = 0.06  # seconds between frames


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_elapsed(seconds: float) -> str:
    """Human-readable elapsed time: '3s', '15s', '1m 6s'."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    return f"{m}m {s}s"


def _format_tokens(n: int) -> str:
    """Compact token count: '847', '1.2k', '12k'."""
    if n < 1000:
        return str(n)
    k = n / 1000
    if k < 10:
        return f"{k:.1f}k"
    return f"{int(k)}k"


# ---------------------------------------------------------------------------
# Spinner
# ---------------------------------------------------------------------------


class PulseSpinner:
    """Thread-safe animated status line with pulse animation.

    Usage::

        with PulseSpinner("Thinking") as spinner:
            # do work …
            spinner.update_tokens(input_tokens=500)
            spinner.set_sub_state("streaming")
    """

    def __init__(self, label: str = "Thinking"):
        self._label = label
        self._sub_state: str = ""
        self._input_tokens: int = 0
        self._output_tokens: int = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_time: float = 0.0

    # -- public API ----------------------------------------------------------

    def set_label(self, label: str) -> None:
        with self._lock:
            self._label = label

    def update_tokens(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """Add to running token totals (thread-safe, additive)."""
        with self._lock:
            self._input_tokens += input_tokens
            self._output_tokens += output_tokens

    def set_sub_state(self, state: str) -> None:
        with self._lock:
            self._sub_state = state

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._start_time = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        # Clear the spinner line
        if hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

    def __enter__(self) -> PulseSpinner:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- internals -----------------------------------------------------------

    def _run(self) -> None:
        """Background animation loop."""
        idx = 0
        use_color = _use_color()
        is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

        if not is_tty:
            # Non-TTY: single static line, no escape codes
            with self._lock:
                label = self._label
            sys.stdout.write(f"  {label}...\n")
            sys.stdout.flush()
            return

        while not self._stop.is_set():
            frame = PULSE_FRAMES[idx % len(PULSE_FRAMES)]
            elapsed = time.monotonic() - self._start_time

            with self._lock:
                label = self._label
                sub = self._sub_state
                in_tok = self._input_tokens
                out_tok = self._output_tokens

            line = self._render_line(frame, label, elapsed, in_tok, out_tok, sub, use_color)
            sys.stdout.write(f"\r\033[K{line}")
            sys.stdout.flush()

            idx += 1
            self._stop.wait(_FRAME_INTERVAL)

    @staticmethod
    def _render_line(
        frame: PulseFrame,
        label: str,
        elapsed: float,
        in_tok: int,
        out_tok: int,
        sub_state: str,
        use_color: bool,
    ) -> str:
        """Build one status line string."""
        # Symbol with colour
        if use_color:
            sym = f"\033[38;2;{frame.r};{frame.g};{frame.b}m{frame.symbol}\033[0m"
        else:
            sym = frame.symbol

        # Assemble detail parts
        parts: list[str] = [_format_elapsed(elapsed)]

        total_tok = in_tok + out_tok
        if total_tok > 0:
            parts.append(f"\u2193 {_format_tokens(total_tok)} tokens")

        if sub_state:
            parts.append(sub_state)

        # Keyboard hint
        parts.append("esc to interrupt")

        detail = " \u00b7 ".join(parts)  # middle dot separator

        if use_color:
            # label in dim white, detail in dim
            return f"  {sym} \033[2m{label}\u2026\033[0m \033[2m({detail})\033[0m"
        return f"  {frame.symbol} {label}... ({detail})"
