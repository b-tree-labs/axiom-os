# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Who may hold a pull request: the lander, or the auto-closer.

A fresh claim blocks the other actor. The file is local state. The
single-flight lock around the rewrite is the switchplan file lease, so
two processes cannot replace each other's claims.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from axiom.infra.switchplan.lock import SingleFlight

_ACTORS = frozenset({"lander", "auto_closer"})


def claims_path(state_dir: Path) -> Path:
    return Path(state_dir) / "agents" / "rivet" / "pr-actors.json"


def _key(repo: str, number: int) -> str:
    return f"{repo}#{number}" if repo else f"#{number}"


def _fresh(record: dict, *, now: datetime, ttl_s: int) -> bool:
    raw = record.get("at")
    if not isinstance(raw, str):
        return False
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return (now - at).total_seconds() <= ttl_s


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


def _mutate(state_dir: Path, mutator) -> bool:
    path = claims_path(state_dir)
    lease = Path(state_dir) / "agents" / "rivet" / "pr-actor-leases"
    flight = SingleFlight("release.pr-actors", lease_dir=lease)
    if not flight.blocking(2):
        return False
    try:
        data = _read(path)
        mutator(data)
        _write(path, data)
        return True
    finally:
        flight.release()


def claim(
    state_dir: Path,
    *,
    repo: str,
    number: int,
    actor: str,
    head_sha: str = "",
    ttl_s: int = 3600,
) -> bool:
    """Record ``actor`` on this pull request. False when the other actor holds it."""
    if actor not in _ACTORS:
        raise ValueError(f"unknown actor {actor!r}")
    key = _key(repo, number)
    now = datetime.now(UTC)
    outcome = {"ok": False}

    def mutate(data: dict) -> None:
        current = data.get(key)
        if (
            isinstance(current, dict)
            and current.get("actor") not in {None, actor}
            and _fresh(current, now=now, ttl_s=ttl_s)
        ):
            outcome["ok"] = False
            return
        data[key] = {"actor": actor, "head_sha": head_sha, "at": now.isoformat()}
        outcome["ok"] = True

    if not _mutate(state_dir, mutate):
        return False
    return bool(outcome["ok"])


def release(state_dir: Path, *, repo: str, number: int, actor: str) -> None:
    key = _key(repo, number)

    def mutate(data: dict) -> None:
        current = data.get(key)
        if isinstance(current, dict) and current.get("actor") == actor:
            data.pop(key, None)

    _mutate(state_dir, mutate)


def replace_actor_claims(
    state_dir: Path,
    actor: str,
    claims: list[tuple[str, int, str]],
    *,
    ttl_s: int = 3600,
) -> None:
    """Replace every claim ``actor`` holds with ``claims`` of (repo, number, sha)."""
    if actor not in _ACTORS:
        raise ValueError(f"unknown actor {actor!r}")
    now = datetime.now(UTC).isoformat()

    def mutate(data: dict) -> None:
        for key, record in list(data.items()):
            if isinstance(record, dict) and record.get("actor") == actor:
                data.pop(key, None)
        for repo, number, head_sha in claims:
            key = _key(repo, number)
            current = data.get(key)
            if (
                isinstance(current, dict)
                and current.get("actor") not in {None, actor}
                and _fresh(current, now=datetime.now(UTC), ttl_s=ttl_s)
            ):
                continue
            data[key] = {"actor": actor, "head_sha": head_sha, "at": now}

    _mutate(state_dir, mutate)


def held_by(
    state_dir: Path,
    number: int,
    *,
    actor: str,
    repo: str | None = None,
    ttl_s: int = 3600,
) -> bool:
    """True when ``actor`` holds this pull request and the claim is fresh.

    ``repo`` narrows the match. Without it, any repo's claim for ``number``
    counts: the auto-closer often knows the number and not the owner.
    """
    path = claims_path(state_dir)
    data = _read(path)
    now = datetime.now(UTC)
    suffix = f"#{number}"
    for key, record in data.items():
        if not isinstance(record, dict) or record.get("actor") != actor:
            continue
        if not str(key).endswith(suffix):
            continue
        if repo is not None and key not in {_key(repo, number), suffix}:
            continue
        if _fresh(record, now=now, ttl_s=ttl_s):
            return True
    return False
