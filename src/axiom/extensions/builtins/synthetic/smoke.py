# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Smoke journey sets: what a role must pass before and after traffic moves (LOOP-S5, ADR-169).

A smoke manifest names, per role, the journeys that must pass::

    [smoke]
    description = "what a serving role must pass before traffic moves"   # optional

    [[role]]
    name = "serve"
    journeys = ["serve-ready.toml", "serve-gate-holds.toml"]   # relative to the manifest

A journey's own ``base_url`` is replaced by the address under test: the idle slot
before a switch, the front after it, a node the release cut names. The set's verdict is the
worst journey's (``pass`` only when every journey passed; ``unproven`` is not a pass).
Unknown keys, a missing journey file or a role with no journeys are errors, never a
check that silently does not run.
"""

from __future__ import annotations

import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .loader import JourneyError, load_journey
from .model import Journey, roll_up
from .runner import run_journey

_TOP_KEYS = {"smoke", "role"}
_SMOKE_KEYS = {"description"}
_ROLE_KEYS = {"name", "journeys"}


@dataclass(frozen=True)
class SmokeSet:
    """One role's journeys, loaded and checked."""

    role: str
    journeys: tuple[Journey, ...]
    paths: tuple[str, ...]
    manifest: str = ""

    def declaration(self) -> dict[str, Any]:
        """A stable description: what a switch's gate fingerprint covers."""
        return {"role": self.role, "journeys": sorted(self.paths)}


def _unknown(where: str, got: set[str], allowed: set[str]) -> None:
    extra = got - allowed
    if extra:
        raise JourneyError(f"{where}: unknown key(s) {sorted(extra)}; allowed {sorted(allowed)}")


def load_smoke_manifest(path: str | Path) -> dict[str, SmokeSet]:
    """Every role's smoke set, keyed by role. Raises :class:`JourneyError` naming the problem."""
    p = Path(path)
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise JourneyError(f"smoke manifest not found: {p}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise JourneyError(f"{p}: not valid TOML: {exc}") from exc
    _unknown(str(p), set(data), _TOP_KEYS)
    _unknown(f"{p} [smoke]", set(data.get("smoke") or {}), _SMOKE_KEYS)
    out: dict[str, SmokeSet] = {}
    for i, raw in enumerate(data.get("role") or [], 1):
        where = f"{p} [[role]] #{i}"
        if not isinstance(raw, Mapping):
            raise JourneyError(f"{where}: a role is a table")
        _unknown(where, set(raw), _ROLE_KEYS)
        name = str(raw.get("name") or "").strip()
        if not name:
            raise JourneyError(f"{where} needs name")
        if name in out:
            raise JourneyError(f"{where}: role {name!r} is declared twice")
        files = [str(f) for f in raw.get("journeys") or []]
        if not files:
            raise JourneyError(f"{where}: role {name!r} lists no journeys")
        journeys = tuple(load_journey(p.parent / f) for f in files)
        out[name] = SmokeSet(role=name, journeys=journeys, paths=tuple(files), manifest=str(p))
    return out


def smoke_set(path: str | Path, role: str) -> SmokeSet:
    """The set for ``role``. A role the manifest does not name is refused: no gate is not a pass."""
    sets = load_smoke_manifest(path)
    if role not in sets:
        raise JourneyError(
            f"no smoke journeys are declared for role {role!r} in {path}; declare a [[role]] for it "
            f"(declared: {', '.join(sorted(sets)) or 'none'})"
        )
    return sets[role]


def run_smoke(sset: SmokeSet, base_url: str, *, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Run every journey of ``sset`` against ``base_url``. Never raises (the runner does not)."""
    started = datetime.now(UTC).isoformat()
    t0 = time.monotonic()
    results = [run_journey(replace(j, base_url=base_url), env=env) for j in sset.journeys]
    failing = [
        f"{r.journey}: {s.name}" for r in results for s in r.steps if s.status != "pass"
    ]
    return {
        "role": sset.role,
        "base_url": base_url,
        "status": roll_up([r.status for r in results]),
        "started_at": started,
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "failing": failing,
        "journeys": [r.to_dict() for r in results],
    }


__all__ = ["SmokeSet", "load_smoke_manifest", "run_smoke", "smoke_set"]
