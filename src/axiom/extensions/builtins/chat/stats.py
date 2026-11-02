# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What a chat session has cost and how big its context is, stated only as far as it is known (ADR-168).

``stats_report`` and ``context_report`` return plain data; ``render_*`` turn it into text. ``/stats``,
``/usage``, ``/context`` and ``--json`` output (the form an agent or MCP caller reads) all go through
them, so they cannot disagree. Three honesty rules apply throughout:

* a cost that cannot be priced is ``unpriced``, never ``$0.00``, and totals say how many turns that is;
* a measurement that was not taken is ``-``, never ``0``;
* an estimate is labelled an estimate, and a context window nobody configured is "unknown".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .usage import CHARS_PER_TOKEN, TurnUsage, UsageTracker, format_cost


@dataclass(frozen=True)
class TurnRow:
    """One user turn: the sum of the model calls it took."""

    index: int
    calls: int
    providers: tuple[str, ...]
    models: tuple[str, ...]
    input_tokens: int
    output_tokens: int
    tool_calls: int | None  # None if any call's count is unknown
    latency_ms: float | None  # model time; None if any call was not timed
    cost: float | None  # None if any call is unpriced


def _distinct(values) -> tuple[str, ...]:
    out: list[str] = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return tuple(out)


def _sum_or_none(values: list[Any]) -> Any:
    return None if any(v is None for v in values) else sum(values)


def turn_rows(tracker: UsageTracker) -> list[TurnRow]:
    """Group the tracker's calls by user turn. Calls with no turn each stand alone."""
    groups: list[list[TurnUsage]] = []
    by_turn: dict[int, list[TurnUsage]] = {}
    for u in tracker.turns:
        if u.turn:
            if u.turn not in by_turn:
                by_turn[u.turn] = []
                groups.append(by_turn[u.turn])
            by_turn[u.turn].append(u)
        else:
            groups.append([u])
    rows = []
    for i, g in enumerate(groups, 1):
        rows.append(
            TurnRow(
                index=i,
                calls=len(g),
                providers=_distinct(u.provider for u in g),
                models=_distinct(u.model for u in g),
                input_tokens=sum(u.input_tokens for u in g),
                output_tokens=sum(u.output_tokens for u in g),
                tool_calls=_sum_or_none([u.tool_calls for u in g]),
                latency_ms=_sum_or_none([u.latency_ms for u in g]),
                cost=_sum_or_none([u.cost for u in g]),
            )
        )
    return rows


def stats_report(agent: Any) -> dict[str, Any]:
    """The session's per-turn usage as data (the shape ``/stats --json`` prints)."""
    tracker: UsageTracker = agent.usage
    rows = turn_rows(tracker)
    timed = [r for r in rows if r.latency_ms is not None]
    slowest = max(timed, key=lambda r: r.latency_ms) if timed else None
    return {
        "turns": [
            {
                "turn": r.index,
                "calls": r.calls,
                "providers": list(r.providers),
                "models": list(r.models),
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "tool_calls": r.tool_calls,
                "latency_ms": r.latency_ms,
                "cost": r.cost,
                "priced": r.cost is not None,
            }
            for r in rows
        ],
        "totals": {
            "turns": len(rows),
            "calls": tracker.turn_count,
            "input_tokens": tracker.total_input_tokens,
            "output_tokens": tracker.total_output_tokens,
            "tool_calls": _sum_or_none([r.tool_calls for r in rows]),
            "latency_ms": _sum_or_none([r.latency_ms for r in rows]),
            "cost_priced": tracker.total_cost,
            "unpriced_turns": tracker.unpriced_turns,
            "untimed_turns": len(rows) - len(timed),
        },
        "slowest_turn": slowest.index if slowest else None,
    }


def _ms(v: float | None) -> str:
    return "-" if v is None else f"{v:,.0f}ms"


def _n(v: int | None) -> str:
    return "-" if v is None else str(v)


def render_stats(agent: Any) -> str:
    """The table ``/stats`` prints."""
    from axiom.infra.cli_format import Column, table
    from axiom.infra.text_utils import gutter, header

    rep = stats_report(agent)
    if not rep["turns"]:
        return "\n  No usage data yet.\n"
    tr = agent.usage
    rows = [
        (
            str(t["turn"]),
            ", ".join(t["providers"]) or "-",
            ", ".join(t["models"]) or "-",
            f"{t['input_tokens']:,}",
            f"{t['output_tokens']:,}",
            _n(t["tool_calls"]),
            _ms(t["latency_ms"]),
            format_cost(t["cost"]),
        )
        for t in rep["turns"]
    ]
    tot = rep["totals"]
    rows.append(
        (
            "all",
            "",
            "",
            f"{tot['input_tokens']:,}",
            f"{tot['output_tokens']:,}",
            _n(tot["tool_calls"]),
            _ms(tot["latency_ms"]),
            tr.cost_label(),
        )
    )
    lines = [gutter(header("Stats"))]
    lines.extend(
        table(
            rows,
            [
                Column("#", align="right"),
                Column("provider", wrap=True),
                Column("model", wrap=True),
                Column("in", align="right"),
                Column("out", align="right"),
                Column("tools", align="right"),
                Column("latency", align="right"),
                Column("cost", align="right", wrap=True),
            ],
            width=96,
            headers=True,
        )
    )
    lines.append("")
    if rep["slowest_turn"] is not None:
        s = next(t for t in rep["turns"] if t["turn"] == rep["slowest_turn"])
        lines.append(f"  Slowest turn: #{s['turn']}, {_ms(s['latency_ms'])} of model time")
    gaps = []
    if tot["untimed_turns"]:
        gaps.append(f"{tot['untimed_turns']} turn(s) were not timed (shown as -)")
    if tot["unpriced_turns"]:
        gaps.append(
            f"{tot['unpriced_turns']} turn(s) unpriced: no price is configured for the model; "
            "the cost total covers only the priced turns"
        )
    for g in gaps:
        lines.append(f"  Note: {g}")
    lines.append("  Latency is model time summed over a turn's calls, not wall-clock time.")
    from axiom.infra.text_utils import surface_block

    return surface_block(lines)


# ---- context --------------------------------------------------------------------------------------


def _answering_provider(agent: Any) -> Any:
    gw = getattr(agent, "gateway", None)
    chosen = getattr(gw, "last_selected", None)
    if chosen is not None:
        return chosen
    try:
        return gw.preview_provider(
            str((getattr(getattr(agent, "session", None), "context", None) or {}).get("content_tier", "any"))
        )
    except Exception:  # noqa: BLE001 - a report must never raise
        return None


def context_report(agent: Any) -> dict[str, Any]:
    """The conversation's size against the active model's window, with every unknown left unknown."""
    messages = agent.session.messages
    convo_chars = sum(len(getattr(m, "content", "") or "") for m in messages)
    convo_tokens = convo_chars // CHARS_PER_TOKEN
    overhead = getattr(getattr(agent, "usage", None), "prompt_overhead", None)
    system_tokens = overhead["system_chars"] // CHARS_PER_TOKEN if overhead else None
    tools_tokens = overhead["tools_chars"] // CHARS_PER_TOKEN if overhead else None
    estimate = convo_tokens + (system_tokens or 0) + (tools_tokens or 0)
    provider = _answering_provider(agent)
    configured = getattr(provider, "context_window", 0)
    window = configured if isinstance(configured, int) and configured > 0 else None
    turns = getattr(getattr(agent, "usage", None), "turns", None) or []
    measured = turns[-1].input_tokens if turns and turns[-1].input_tokens else None
    return {
        "messages": len(messages),
        "conversation_tokens_est": convo_tokens,
        "system_tokens_est": system_tokens,
        "tools_tokens_est": tools_tokens,
        "tool_count": overhead["tool_count"] if overhead else None,
        "total_tokens_est": estimate,
        "last_call_input_tokens": measured,
        "model": getattr(provider, "model", "") or "",
        "window": window,
        "percent_est": round(100.0 * estimate / window, 1) if window else None,
    }


def render_context(agent: Any) -> str:
    from axiom.infra.text_utils import bar, gutter, header, surface_block
    from axiom.setup.renderer import _c, _Colors

    r = context_report(agent)
    pairs = [("Messages:", str(r["messages"]))]
    pairs.append(("Conversation:", f"~{r['conversation_tokens_est']:,} tokens (estimate)"))
    if r["system_tokens_est"] is None:
        pairs.append(("Overhead:", "unknown until a turn has run (system prompt and tool schemas)"))
    else:
        pairs.append(("System prompt:", f"~{r['system_tokens_est']:,} tokens (estimate)"))
        pairs.append(
            ("Tool schemas:", f"~{r['tools_tokens_est']:,} tokens (estimate, {r['tool_count']} tools)")
        )
    pairs.append(("Est. tokens:", f"~{r['total_tokens_est']:,} tokens (estimate, {CHARS_PER_TOKEN} chars/token)"))
    if r["last_call_input_tokens"] is not None:
        pairs.append(("Last call:", f"{r['last_call_input_tokens']:,} input tokens (measured by the provider)"))
    if r["window"]:
        model = f" ({r['model']})" if r["model"] else ""
        pairs.append(("Window:", f"{r['window']:,} tokens{model}"))
        pairs.append(("Usage:", f"{bar(min(100.0, r['percent_est']), width=20)} ~{r['percent_est']:.1f}% (estimate)"))
    else:
        pairs.append(("Window:", "unknown: this model's context window is not configured"))
    pad = max(len(label) for label, _ in pairs) + 2
    lines = [gutter(header("Context"))]
    for label, value in pairs:
        lines.append(f"  {_c(_Colors.BOLD, label):<{pad + len(_c(_Colors.BOLD, label)) - len(label)}}{value}")
    return surface_block(lines)
