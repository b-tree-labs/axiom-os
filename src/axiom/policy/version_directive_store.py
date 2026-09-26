# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""File-backed store for version directives.

A version directive is a scoped, auditable request that a set of nodes
install at least a given version of a package by a deadline. This is the
federation receipt target for the `@all-curios:<context>` upgrade pattern
described in project_nl_policy_broadcasting.md — the transport layer
calls `add()` on receipt; the node's Tidy reads via `load_active()` and
files a finding for non-compliance.

Storage is a single JSONL file under the user state directory so the
record survives process restarts. No PolicyEngine coupling yet — when
PolicyEngine grows persistence, the two merge.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from axiom.infra.paths import get_user_state_dir


def _default_store_path() -> Path:
    return get_user_state_dir() / "policy" / "version_directives.jsonl"


@dataclass
class VersionDirective:
    """A single version-requirement directive."""

    package: str
    min_version: str
    issuer: str  # principal handle of the issuer
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    deadline: str = ""  # ISO date, optional
    scope: str = ""  # free-form scope id (e.g. a period id); empty = ambient
    reason: str = ""  # optional human explanation
    issued_at: float = field(default_factory=time.time)
    active: bool = True
    revoked_at: float | None = None
    revocation_reason: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> VersionDirective:
        data = json.loads(line)
        return cls(**data)


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def _read_all(path: Path | None = None) -> list[VersionDirective]:
    p = path or _default_store_path()
    if not p.exists():
        return []
    out: list[VersionDirective] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(VersionDirective.from_json(line))
        except Exception:
            # Skip malformed lines; do not crash Tidy's health check.
            continue
    return out


def _write_all(records: list[VersionDirective], path: Path | None = None) -> None:
    p = path or _default_store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(r.to_json() + "\n")
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def add(directive: VersionDirective, *, path: Path | None = None) -> str:
    """Persist a directive. Returns its id."""
    records = _read_all(path)
    records.append(directive)
    _write_all(records, path)
    return directive.id


def list_all(*, path: Path | None = None) -> list[VersionDirective]:
    return _read_all(path)


def load_active(*, now: float | None = None, path: Path | None = None) -> list[VersionDirective]:
    """Return non-revoked directives whose deadline has not passed.

    `now` is injectable for testing. A directive with no deadline is
    always considered current until explicitly revoked.
    """
    import datetime as _dt

    now = now if now is not None else time.time()
    active: list[VersionDirective] = []
    for d in _read_all(path):
        if not d.active:
            continue
        if d.deadline:
            try:
                # ISO 8601; accept date or datetime
                dl = _dt.datetime.fromisoformat(d.deadline)
                if dl.tzinfo is None:
                    dl = dl.replace(tzinfo=_dt.UTC)
                if dl.timestamp() < now:
                    continue  # expired
            except ValueError:
                pass  # malformed deadline — treat as no deadline
        active.append(d)
    return active


def revoke(directive_id: str, *, reason: str = "", path: Path | None = None) -> bool:
    """Mark a directive revoked. Returns True if found and revoked."""
    records = _read_all(path)
    found = False
    for i, d in enumerate(records):
        if d.id == directive_id and d.active:
            records[i] = VersionDirective(
                **{
                    **asdict(d),
                    "active": False,
                    "revoked_at": time.time(),
                    "revocation_reason": reason,
                }
            )
            found = True
            break
    if found:
        _write_all(records, path)
    return found
