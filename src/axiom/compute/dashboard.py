# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TUI dashboard for live `dispatch_streaming` runs.

Per Twin Toolkit Demo Spec §9.1 — the live run dashboard layout.

Two layers:
1. **DashboardState** — pure-data snapshot computed from event history +
   watch verdicts. Fully deterministic + testable.
2. **render_dashboard_snapshot** — turns DashboardState into a Rich-rendered
   string (or live-updates a Rich Live panel). Smoke-tested only;
   visual-correctness is manual review.

Phase 3c ships state computation + a string renderer. The Rich Live
integration (interactive dashboard during a real run) is one more layer
of glue using the same DashboardState; covered in Phase 3d if needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from axiom.compute.events import (
    ConditionVerdict,
    EventKind,
    KernelEvent,
    WatchCondition,
)


# Trajectory keeps the last N k-eff values for sparkline rendering.
_TRAJECTORY_WINDOW = 20


@dataclass(frozen=True)
class DashboardState:
    """Snapshot of run state computed from event history.

    Pure data; renderable by render_dashboard_snapshot or any other UI layer.
    """

    cycles_completed: int = 0
    inactive_cycles: int = 0
    active_cycles: int = 0
    last_k_eff: float | None = None
    last_k_eff_std: float | None = None
    k_eff_trajectory: list[float] = field(default_factory=list)
    last_shannon_entropy: float | None = None
    lost_particles: int = 0
    tally_count: int = 0
    tallies: dict[str, float] = field(default_factory=dict)  # name → latest rel_err
    triggered_watches: list[tuple[str, ConditionVerdict]] = field(default_factory=list)
    last_event_timestamp_ms: int | None = None


def compute_dashboard_state(
    events: list[KernelEvent],
    watch_conditions: list[WatchCondition],
) -> DashboardState:
    """Pure function: event history + conditions → renderable state."""
    cycle_events = [e for e in events if e.kind == EventKind.CYCLE_COMPLETE]
    tally_events = [e for e in events if e.kind == EventKind.TALLY_UPDATE]
    lost_events = [e for e in events if e.kind == EventKind.LOST_PARTICLE]

    # Cycle-derived state
    inactive_cycles = sum(1 for e in cycle_events if e.payload.get("phase") == "inactive")
    active_cycles = sum(1 for e in cycle_events if e.payload.get("phase") == "active")
    last_k_eff: float | None = None
    last_k_eff_std: float | None = None
    last_entropy: float | None = None
    if cycle_events:
        last = cycle_events[-1].payload
        last_k_eff = last.get("k_eff")
        last_k_eff_std = last.get("k_eff_std")
        last_entropy = last.get("shannon_entropy")
    trajectory = [
        float(e.payload.get("k_eff"))
        for e in cycle_events[-_TRAJECTORY_WINDOW:]
        if e.payload.get("k_eff") is not None
    ]

    # Tally-derived state
    tallies: dict[str, float] = {}
    for event in tally_events:
        name = event.payload.get("tally_name", "<unknown>")
        rel_err = event.payload.get("rel_err")
        if rel_err is not None:
            tallies[name] = float(rel_err)

    # Lost-particles total
    lost_particles = sum(int(e.payload.get("lost_particles", 0)) for e in lost_events)

    # Watch verdicts
    triggered: list[tuple[str, ConditionVerdict]] = []
    for cond in watch_conditions:
        verdict = cond.evaluate(events)
        if verdict.triggered:
            triggered.append((cond.name, verdict))

    last_ts = events[-1].timestamp_ms if events else None

    return DashboardState(
        cycles_completed=len(cycle_events),
        inactive_cycles=inactive_cycles,
        active_cycles=active_cycles,
        last_k_eff=last_k_eff,
        last_k_eff_std=last_k_eff_std,
        k_eff_trajectory=trajectory,
        last_shannon_entropy=last_entropy,
        lost_particles=lost_particles,
        tally_count=len(tallies),
        tallies=tallies,
        triggered_watches=triggered,
        last_event_timestamp_ms=last_ts,
    )


# ----- Rich rendering -----


_SPARKLINE_BLOCKS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[float], width: int = 14) -> str:
    """Render a list of floats as an 8-block Unicode sparkline (max `width` chars)."""
    if not values:
        return ""
    sample = values[-width:]
    lo, hi = min(sample), max(sample)
    if hi == lo:
        return _SPARKLINE_BLOCKS[3] * len(sample)
    return "".join(
        _SPARKLINE_BLOCKS[
            min(
                len(_SPARKLINE_BLOCKS) - 1,
                int((v - lo) / (hi - lo) * (len(_SPARKLINE_BLOCKS) - 1)),
            )
        ]
        for v in sample
    )


def render_dashboard_snapshot(
    state: DashboardState,
    model_id: str,
    peer_id: str,
    kernel: str,
    width: int = 100,
) -> str:
    """Render a DashboardState to a string via Rich.

    Returns the string representation (uses Console.export_text for testability).
    For interactive live updates, callers wrap this in a Rich Live context.
    """
    from io import StringIO
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    buf = StringIO()
    console = Console(file=buf, width=width, force_terminal=False, color_system=None)

    # Header
    header = Text()
    header.append(f"  peer    {peer_id}", style="dim")
    header.append("  ·  ", style="dim")
    header.append(f"kernel    {kernel}", style="dim")
    header.append("\n")
    header.append(f"  model   {model_id}", style="dim")
    if state.last_event_timestamp_ms is not None:
        header.append("  ·  ", style="dim")
        header.append(f"wall    {state.last_event_timestamp_ms / 1000:.1f}s", style="dim")

    # Convergence table
    convergence = Table.grid(padding=(0, 2))
    convergence.add_column(justify="left")
    convergence.add_column(justify="right")
    convergence.add_column(justify="left")
    if state.last_k_eff is not None:
        k_str = f"{state.last_k_eff:.5f}"
        if state.last_k_eff_std is not None:
            k_str += f" ± {state.last_k_eff_std:.5f}"
        spark = _sparkline(state.k_eff_trajectory)
        convergence.add_row("k-effective", k_str, spark)
    if state.last_shannon_entropy is not None:
        convergence.add_row("shannon entropy", f"{state.last_shannon_entropy:.2f}", "")
    convergence.add_row(
        "particle balance",
        f"{state.lost_particles} lost",
        "nominal" if state.lost_particles == 0 else "⚠ check geometry",
    )
    if state.tally_count > 0:
        worst = max(state.tallies.values())
        convergence.add_row("tally health", f"{state.tally_count} tracked", f"worst rel_err {worst:.4f}")

    # Progress
    progress = Text()
    if state.cycles_completed:
        progress.append(
            f"  inactive {state.inactive_cycles}  ·  active {state.active_cycles}  ·  total {state.cycles_completed}",
            style="dim",
        )

    # Watch conditions
    watches = Text()
    if state.triggered_watches:
        for name, verdict in state.triggered_watches:
            severity = verdict.severity
            glyph = "⛔" if severity == "stop_worthy" else ("⚠" if severity == "watch" else "ℹ")
            watches.append(f"  {glyph}  ", style="bold red" if severity == "stop_worthy" else "bold yellow")
            watches.append(f"{name}  ", style="bold")
            watches.append(f"({verdict.classification})\n", style="dim")
    else:
        watches.append("  · all always-auto-stop conditions inactive", style="dim")

    # Compose a single panel
    body = Text()
    body.append_text(header)
    body.append("\n\n")
    body.append_text(progress)
    body.append("\n")
    body.append("\n")
    # Render convergence table separately
    console.print(Panel(body, title=f"neut model run · {model_id}", border_style="bright_blue"))
    if state.cycles_completed > 0:
        console.print(Panel(convergence, title="Convergence", border_style="green"))
    console.print(Panel(watches, title="Watch conditions", border_style="yellow"))

    return buf.getvalue()
