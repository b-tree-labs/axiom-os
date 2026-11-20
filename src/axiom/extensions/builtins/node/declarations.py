# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What the node's owner tells KEEL that the machine cannot tell it.

Three kinds of declaration, in one TOML file under the extension's state
directory (``<state>/node/declarations.toml``):

- an **engine** Axiom did not install: its name, unit, port, weights
  directory and owner, so the inventory names it instead of guessing;
- a storage **tier**: a mount, ``hot`` or ``bulk``, and the free-space floor
  below which vitals report pressure;
- a **tracked** file: a live path and the source it is supposed to match, so
  drift is measured, not suspected.

Writes go through the platform's atomic state write and every change is
returned as an action so the invoking surface records it (AEOS 1.0 §2.14: the
dedicated configuration primitive is not built yet; this is the durable,
auditable path the platform has today, and the file is read fresh on every
call, never cached).
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

FILE = "declarations.toml"
TIERS = ("hot", "bulk")
KINDS = ("engine", "tier", "tracked")


@dataclass
class Engine:
    name: str
    unit: str = ""
    port: int | None = None
    weights: str = ""
    owner: str = ""
    kind: str = ""
    note: str = ""


@dataclass
class Tier:
    mount: str
    tier: str = "hot"
    floor_gb: int = 0
    #: When this mount is a kubelet nodefs: its hard eviction floor and minimum reclaim.
    kubelet_floor_gb: int = 0
    kubelet_reclaim_gb: int = 0


@dataclass
class Tracked:
    live: str
    source: str = ""


@dataclass
class Declarations:
    engines: list[Engine] = field(default_factory=list)
    tiers: list[Tier] = field(default_factory=list)
    tracked: list[Tracked] = field(default_factory=list)
    config_dirs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "engines": [asdict(e) for e in self.engines],
            "tiers": [asdict(t) for t in self.tiers],
            "tracked": [asdict(t) for t in self.tracked],
            "config_dirs": list(self.config_dirs),
        }


def path_for(state_dir: Path) -> Path:
    return Path(state_dir) / "node" / FILE


def load(state_dir: Path) -> Declarations:
    """The declarations, or an empty set; a damaged file is an empty set that says so."""
    p = path_for(state_dir)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return Declarations()
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return Declarations(config_dirs=[f"unreadable: {p}"])
    d = Declarations()
    for e in doc.get("engines", []) or []:
        if isinstance(e, dict) and e.get("name"):
            d.engines.append(
                Engine(
                    name=str(e["name"]),
                    unit=str(e.get("unit", "")),
                    port=int(e["port"]) if str(e.get("port", "")).isdigit() else None,
                    weights=str(e.get("weights", "")),
                    owner=str(e.get("owner", "")),
                    kind=str(e.get("kind", "")),
                    note=str(e.get("note", "")),
                )
            )
    for t in doc.get("tiers", []) or []:
        if isinstance(t, dict) and t.get("mount"):
            tier = str(t.get("tier", "hot"))
            d.tiers.append(
                Tier(
                    mount=str(t["mount"]),
                    tier=tier if tier in TIERS else "hot",
                    floor_gb=int(t.get("floor_gb", 0) or 0),
                    kubelet_floor_gb=int(t.get("kubelet_floor_gb", 0) or 0),
                    kubelet_reclaim_gb=int(t.get("kubelet_reclaim_gb", 0) or 0),
                )
            )
    for t in doc.get("tracked", []) or []:
        if isinstance(t, dict) and t.get("live"):
            d.tracked.append(Tracked(live=str(t["live"]), source=str(t.get("source", ""))))
    d.config_dirs = [str(x) for x in doc.get("config_dirs", []) or []]
    return d


def dumps(d: Declarations) -> str:
    """TOML, written by hand so the file stays readable and ordered the way it is read."""
    lines = ["# What the node's owner told KEEL. Edit with `node declare`, or by hand.", ""]
    if d.config_dirs:
        lines.append("config_dirs = [" + ", ".join(_q(x) for x in d.config_dirs) + "]")
        lines.append("")
    for e in d.engines:
        lines += ["[[engines]]", f"name = {_q(e.name)}"]
        if e.kind:
            lines.append(f"kind = {_q(e.kind)}")
        if e.unit:
            lines.append(f"unit = {_q(e.unit)}")
        if e.port is not None:
            lines.append(f"port = {int(e.port)}")
        if e.weights:
            lines.append(f"weights = {_q(e.weights)}")
        if e.owner:
            lines.append(f"owner = {_q(e.owner)}")
        if e.note:
            lines.append(f"note = {_q(e.note)}")
        lines.append("")
    for t in d.tiers:
        lines += [
            "[[tiers]]",
            f"mount = {_q(t.mount)}",
            f"tier = {_q(t.tier)}",
            f"floor_gb = {int(t.floor_gb)}",
        ]
        if t.kubelet_floor_gb:
            lines += [
                f"kubelet_floor_gb = {int(t.kubelet_floor_gb)}",
                f"kubelet_reclaim_gb = {int(t.kubelet_reclaim_gb)}",
            ]
        lines.append("")
    for t in d.tracked:
        lines += ["[[tracked]]", f"live = {_q(t.live)}"]
        if t.source:
            lines.append(f"source = {_q(t.source)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save(state_dir: Path, d: Declarations) -> Path:
    from axiom.infra.state_integrity import atomic_write_text

    p = path_for(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, dumps(d))
    return p


def declare(state_dir: Path, kind: str, **fields: Any) -> tuple[Declarations, str]:
    """Add or replace one declaration; returns the new set and one sentence saying what changed."""
    if kind not in KINDS:
        raise ValueError(f"not a declaration kind: {kind!r}; the kinds are {', '.join(KINDS)}")
    d = load(state_dir)
    if kind == "engine":
        name = str(fields.get("name") or "").strip()
        if not name:
            raise ValueError("an engine declaration needs a name")
        port = fields.get("port")
        e = Engine(
            name=name,
            unit=str(fields.get("unit") or ""),
            port=int(port) if port not in (None, "") else None,
            weights=str(fields.get("weights") or ""),
            owner=str(fields.get("owner") or ""),
            kind=str(fields.get("engine_kind") or ""),  # `kind` itself is the declaration kind
            note=str(fields.get("note") or ""),
        )
        replaced = any(x.name == name for x in d.engines)
        d.engines = [x for x in d.engines if x.name != name] + [e]
        what = f"engine {name} {'updated' if replaced else 'declared'}" + (
            f" on port {e.port}" if e.port else ""
        )
    elif kind == "tier":
        mount = str(fields.get("mount") or "").strip()
        tier = str(fields.get("tier") or "hot")
        if not mount:
            raise ValueError("a tier declaration needs a mount")
        if tier not in TIERS:
            raise ValueError(f"a tier is one of {', '.join(TIERS)}, not {tier!r}")
        t = Tier(
            mount=mount,
            tier=tier,
            floor_gb=int(fields.get("floor_gb") or 0),
            kubelet_floor_gb=int(fields.get("kubelet_floor_gb") or 0),
            kubelet_reclaim_gb=int(fields.get("kubelet_reclaim_gb") or 0),
        )
        replaced = any(x.mount == mount for x in d.tiers)
        d.tiers = [x for x in d.tiers if x.mount != mount] + [t]
        what = f"{mount} {'updated' if replaced else 'declared'} as the {tier} tier" + (
            f" with a {t.floor_gb} GB floor" if t.floor_gb else ""
        )
    else:
        live = str(fields.get("live") or "").strip()
        if not live:
            raise ValueError("a tracked declaration needs the live path")
        t = Tracked(live=live, source=str(fields.get("source") or ""))
        replaced = any(x.live == live for x in d.tracked)
        d.tracked = [x for x in d.tracked if x.live != live] + [t]
        what = f"{live} {'updated' if replaced else 'declared'} as tracked" + (
            f" from {t.source}" if t.source else " (no source yet)"
        )
    save(state_dir, d)
    return d, what


def _q(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


__all__ = [
    "Declarations",
    "Engine",
    "FILE",
    "KINDS",
    "TIERS",
    "Tier",
    "Tracked",
    "declare",
    "dumps",
    "load",
    "path_for",
    "save",
]
