# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One human renderer per observation family (ADR-265 D7).

Every CLI that prints an observation prints it through here, so the same observation
reads the same wherever it is printed. ``--json`` never comes here: it emits the
envelope unchanged.

- A **reading** says its value, its unit and how sure it is, and its quality only when
  that is not ``good``. A modelled value says it is not a measurement.
- A **statement** says what was claimed and who is accountable for it; an agent's claim
  also says how sure the agent is ("confidence not yet known" until calibrated, D10).
- An **event** says what happened, its phase and severity, and what it was about.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from . import MODELLED_CLASSES, Event, Observation, Reading, Statement, uncertainty_words


def _num(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _when(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")


def reading_text(value: Any, unit: str | None, uncertainty: Any, *, quality: str | None = None) -> str:
    """One reading as people read it: ``312.4 K ± 0.5 K``, never a bare number."""
    if value is None:
        return "no value"
    u = f" {unit}" if unit else ""
    words = uncertainty_words(uncertainty, unit)
    out = f"{_num(value)}{u} {words}" if words != "not reported" else f"{_num(value)}{u} (uncertainty not reported)"
    if quality and quality != "good":
        out += f", {quality}"
    return out


def _about(obs: Observation) -> str:
    return ", ".join(str(s) for s in obs.subjects)


def render_text(obs: Observation) -> str:
    """The one line for an observation of any family."""
    c = obs.content
    if isinstance(c, Reading):
        line = f"{_about(obs)}  {reading_text(c.value, c.unit, c.uncertainty, quality=c.quality)}  at {_when(obs.observed_at)}"
        if obs.source_class in MODELLED_CLASSES and obs.derivation:
            d = obs.derivation
            line += f"  ({obs.source_class} by {d.method}{f' v{d.version}' if d.version else ''})"
        return line
    if isinstance(c, Statement):
        who = obs.source.principal
        if obs.source.agent:
            who = f"{obs.source.agent} for {who}"
        parts = [who]
        if obs.confidence is not None:
            parts.append(obs.confidence.words())
        parts.append(_when(obs.observed_at))
        return f"{c.kind}: {c.text}  ({', '.join(parts)})"
    if isinstance(c, Event):
        return f"{c.kind} {c.phase} ({c.severity})  {_about(obs)}  at {_when(obs.observed_at)}"
    raise TypeError(f"no renderer for {type(c).__name__}")


__all__ = ["reading_text", "render_text"]
