# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Scaffold-graduation registry (issue #202.6 + #201.6).

`axi ext init` scaffolds a new AEOS-conformant extension. Sometimes
the author iterates, lands the work, and graduates the scaffold.
Sometimes the scaffold sits untouched for weeks — the 2026-05
`chat_agent/` was the motivating case (one source file, abandoned).

This module tracks each scaffold's creation moment + graduation
state. The companion hygiene signal
`hygiene.git_signals.check_non_graduated_scaffolds` surfaces stale
non-graduated scaffolds as Findings so they don't accumulate.

Storage shape:

  `<project_root>/.axi/scaffold-graduation.json` — a JSON array of
  `{name, path, created_at, graduated_at}` records.

Per-project state (not user-global) so multi-project developers see
their scaffolds without cross-project bleed. Idempotent by name: a
re-recording overwrites the existing entry rather than appending.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

REGISTRY_RELATIVE_PATH = Path(".axi") / "scaffold-graduation.json"


@dataclass(frozen=True)
class ScaffoldRecord:
    """One scaffold's lifecycle row."""

    name: str
    path: str  # relative to project_root
    created_at: str
    graduated_at: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "ScaffoldRecord":
        return cls(
            name=data["name"],
            path=data["path"],
            created_at=data["created_at"],
            graduated_at=data.get("graduated_at"),
        )


def _registry_path(project_root: Path) -> Path:
    return project_root / REGISTRY_RELATIVE_PATH


def _read_records(project_root: Path) -> list[ScaffoldRecord]:
    p = _registry_path(project_root)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    out: list[ScaffoldRecord] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        try:
            out.append(ScaffoldRecord.from_dict(row))
        except KeyError:
            continue
    return out


def _write_records(project_root: Path, records: list[ScaffoldRecord]) -> None:
    p = _registry_path(project_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps([asdict(r) for r in records], indent=2, sort_keys=True)
    )


def list_records(project_root: Path) -> list[ScaffoldRecord]:
    """Return every recorded scaffold for the project (graduated + not)."""
    return _read_records(project_root)


def list_non_graduated(project_root: Path) -> list[ScaffoldRecord]:
    """Return scaffolds that have not yet been graduated."""
    return [r for r in _read_records(project_root) if r.graduated_at is None]


def record_scaffold(project_root: Path, *, name: str, ext_path: Path) -> ScaffoldRecord:
    """Record a new scaffold (or refresh an existing entry's timestamp).

    Idempotent by ``name``: re-recording overwrites the existing record
    rather than duplicating — the user re-scaffolding intends to start
    over, not append.
    """
    now = datetime.now(UTC).isoformat()
    try:
        rel = ext_path.relative_to(project_root)
    except ValueError:
        rel = ext_path
    new = ScaffoldRecord(name=name, path=str(rel), created_at=now)
    records = _read_records(project_root)
    records = [r for r in records if r.name != name]
    records.append(new)
    _write_records(project_root, records)
    return new


def graduate_scaffold(project_root: Path, *, name: str) -> ScaffoldRecord:
    """Mark a scaffold as graduated. Raises KeyError if no record exists."""
    now = datetime.now(UTC).isoformat()
    records = _read_records(project_root)
    for i, rec in enumerate(records):
        if rec.name == name:
            updated = ScaffoldRecord(
                name=rec.name,
                path=rec.path,
                created_at=rec.created_at,
                graduated_at=now,
            )
            records[i] = updated
            _write_records(project_root, records)
            return updated
    raise KeyError(f"no scaffold recorded with name {name!r}")
