# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Forge claim labels for one lander.

The label on the pull request is what other actors see. This file
records when it was applied so a crashed pass does not hold the label
past the claim TTL. Foreign labels are not written here; the actor
skips a pull request that already carries one.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path


def claims_path(state_dir: Path) -> Path:
    return Path(state_dir) / "agents" / "rivet" / "claim-labels.json"


def _key(repo: str, number: int) -> str:
    return f"{repo}#{number}"


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def claim_record(state_dir: Path, repo: str, number: int) -> dict | None:
    record = _read(claims_path(state_dir)).get(_key(repo, number))
    return record if isinstance(record, dict) else None


def record_claim(
    state_dir: Path,
    *,
    repo: str,
    number: int,
    label: str,
    head_sha: str,
    now: datetime | None = None,
) -> None:
    path = claims_path(state_dir)
    data = _read(path)
    moment = now or datetime.now(UTC)
    data[_key(repo, number)] = {
        "label": label,
        "head_sha": head_sha,
        "at": moment.isoformat(),
    }
    _write(path, data)


def release_claim(state_dir: Path, *, repo: str, number: int) -> None:
    path = claims_path(state_dir)
    data = _read(path)
    if _key(repo, number) in data:
        data.pop(_key(repo, number), None)
        _write(path, data)


def claim_expired(
    record: dict | None,
    *,
    ttl_s: int,
    now: datetime | None = None,
) -> bool:
    """True when a recorded claim is older than ``ttl_s``.

    A missing record is not expired; nothing was held. Fresh means the
    age is within the TTL, matching the local actor claims.
    """
    if not record:
        return False
    raw = record.get("at")
    if not isinstance(raw, str):
        return True
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    moment = now or datetime.now(UTC)
    return (moment - at).total_seconds() > ttl_s
