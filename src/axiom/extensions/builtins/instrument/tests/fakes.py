# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Stand-in instrument kinds, entry points and extensions for the instrument tests.

The kind is scalar-shaped on purpose (ADR-256 D6): it yields readings with a unit,
and has no frames, no bus, no triggers, no source kind and no connector file. If
the contract only fits video, these tests are where it shows.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from axiom.extensions.builtins.instrument.kinds import InstrumentRow, KindStatus

#: Where this module is importable from, for entry points and manifest blocks.
HERE = "axiom.extensions.builtins.instrument.tests.fakes"


@dataclass
class GaugeKind:
    """Readings with a unit from a stand-in meter: two channels, temperature and power."""

    name: str = "gauges"
    description: str = "Readings with a unit from a stand-in meter"
    yields: str = "readings"
    skill_for_verb: dict[str, str] = field(
        default_factory=lambda: {"read": "gauges.read", "list": "gauges.list"}
    )
    channels: dict[str, str] = field(default_factory=lambda: {"temp": "degC", "power": "W"})
    calls: list[tuple[list[str], str]] = field(default_factory=list)

    def build_parser(self, prog: str) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog=prog, description="Stand-in gauges")
        sub = parser.add_subparsers(dest="verb")
        read = sub.add_parser("read", help="read one channel")
        read.add_argument("channel")
        read.add_argument("--unit", choices=["degC", "W"], help="the unit to report in")
        sub.add_parser("list", help="the channels")
        return parser

    def main(self, argv: list[str], prog: str) -> int:
        self.calls.append((list(argv), prog))
        args = self.build_parser(prog).parse_args(argv)
        if args.verb == "read":
            if args.channel not in self.channels:
                print(f"no channel {args.channel!r}; `{prog} list` shows them")
                return 3
            print(f"{args.channel} 21.5 {self.channels[args.channel]}")
            return 0
        print("\n".join(self.channels))
        return 0

    def list(self) -> list[InstrumentRow]:
        return [
            InstrumentRow(kind=self.name, name=ch, health="ready", state="reading", detail=unit)
            for ch, unit in self.channels.items()
        ]

    def status(self) -> KindStatus:
        n = len(self.channels)
        return KindStatus(
            kind=self.name, ok=True, summary=f"{n} channels, {n} ready", counts={"ready": n}
        )


class SulkingKind(GaugeKind):
    """Registers fine, then fails every summary it is asked for."""

    def __init__(self) -> None:
        super().__init__(name="sulky", description="Answers nothing")

    def list(self) -> list[InstrumentRow]:
        raise RuntimeError("the meter is not answering")

    def status(self) -> KindStatus:
        raise RuntimeError("the meter is not answering")


class NotAKind:
    """Has a name and nothing else of the contract."""

    name = "gauges"


#: What entry points and manifest blocks point at.
GAUGES = GaugeKind()
OTHER_GAUGES = GaugeKind()
WRONG_NAME = GaugeKind(name="meters")
NOT_A_KIND = NotAKind()


def make_gauges() -> GaugeKind:
    """A zero-argument factory, the other form an entry may take."""
    return GaugeKind(name="made")


def explode() -> GaugeKind:
    raise RuntimeError("this package is broken")


def entry_point(name: str, value: str, dist: str | None) -> SimpleNamespace:
    """An ``importlib.metadata.EntryPoint`` look-alike: what discovery reads of one."""
    return SimpleNamespace(
        name=name, value=value, dist=SimpleNamespace(name=dist) if dist else None
    )


def extension(root: Path, blocks: list[dict[str, Any]], *, name: str = "gauges-ext") -> Any:
    """An extension at ``root`` whose manifest declares ``blocks`` as ``[[instrument_kinds]]``."""
    root.mkdir(parents=True, exist_ok=True)
    lines = ["[extension]", f'name = "{name}"', 'version = "0.1.0"', ""]
    for block in blocks:
        lines.append("[[instrument_kinds]]")
        lines += [f'{k} = "{v}"' for k, v in block.items()]
        lines.append("")
    (root / "axiom-extension.toml").write_text("\n".join(lines), encoding="utf-8")
    return SimpleNamespace(name=name, root=root, enabled=True)


__all__ = [
    "GAUGES",
    "GaugeKind",
    "HERE",
    "NOT_A_KIND",
    "NotAKind",
    "OTHER_GAUGES",
    "SulkingKind",
    "WRONG_NAME",
    "entry_point",
    "explode",
    "extension",
    "make_gauges",
]
