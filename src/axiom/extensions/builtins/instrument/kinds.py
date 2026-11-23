# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``InstrumentKind``: what a kind gives the ``instrument`` noun, and nothing more (ADR-256 D6).

A kind is defined by what it quantifies into (frames, readings), not by the
physical quantity. The contract is the control surface only: a name, a line of
description, what it yields, its own command line, and two summaries. It names no
frame, bus, trigger, source kind or connector file, so a kind that yields readings
with a unit fits it as well as video does. A kind's data path, lifecycle and
dependencies stay its own.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable

#: An instrument's health, the connector vocabulary (ADR-220 §3).
HEALTH_STATES = ("ready", "degraded", "failed", "unmounted")


@dataclass(frozen=True)
class InstrumentRow:
    """One declared instrument, as ``instrument list`` shows it. Never a credential."""

    kind: str
    name: str
    health: str
    state: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class KindStatus:
    """One kind's summary, as ``instrument status`` shows it.

    ``fix`` names the command the person typed (the active brand's program).
    """

    kind: str
    ok: bool
    summary: str
    counts: Mapping[str, int] = field(default_factory=dict)
    error: str | None = None
    fix: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["counts"] = dict(self.counts)
        return out


@runtime_checkable
class InstrumentKind(Protocol):
    """What a kind registers (spec-instrument §``InstrumentKind``).

    ``prog`` is the full command path the person typed (``acme instrument video``
    under a product branded ``acme``), so the kind's usage lines name it.
    ``skill_for_verb`` maps each verb path after the kind's name (``"add"``,
    ``"source rotate"``) to the skill it wraps (``"video.add"``): skill names follow
    the kind's own extension (ADR-256 D5).
    """

    name: str
    description: str
    yields: str
    skill_for_verb: Mapping[str, str]

    def build_parser(self, prog: str) -> argparse.ArgumentParser: ...

    def main(self, argv: list[str], prog: str) -> int: ...

    def list(self) -> list[InstrumentRow]: ...

    def status(self) -> KindStatus: ...


__all__ = ["HEALTH_STATES", "InstrumentKind", "InstrumentRow", "KindStatus"]
