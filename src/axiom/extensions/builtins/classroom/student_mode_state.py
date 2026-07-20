# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Student-side mode-policy cache + preference persistence.

Students fetch the instructor's `ClassroomModePolicy` from the
coordinator at ask-time, cache it locally, and also remember their
own preferred mode across invocations so they don't have to pass
``--mode`` on every command.

Disk layout under ``~/.axi/classrooms/<classroom_id>/``::

    policy.json           — most recently fetched classroom policy
    mode_preference.txt   — one line: the student's last mode choice
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from .learning_modes import ClassroomModePolicy

# ---------------------------------------------------------------------------
# Local cache
# ---------------------------------------------------------------------------


def _policy_path(classroom_dir: Path) -> Path:
    return classroom_dir / "policy.json"


def _preference_path(classroom_dir: Path) -> Path:
    return classroom_dir / "mode_preference.txt"


def load_cached_policy(classroom_dir: Path) -> ClassroomModePolicy | None:
    path = _policy_path(classroom_dir)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    return ClassroomModePolicy.from_dict(raw)


def save_cached_policy(
    classroom_dir: Path, policy: ClassroomModePolicy,
) -> None:
    classroom_dir.mkdir(parents=True, exist_ok=True)
    _policy_path(classroom_dir).write_text(
        json.dumps(policy.to_dict(), indent=2)
    )


def load_preference(classroom_dir: Path) -> str | None:
    path = _preference_path(classroom_dir)
    if not path.is_file():
        return None
    val = path.read_text().strip()
    return val or None


def save_preference(classroom_dir: Path, mode_name: str) -> None:
    classroom_dir.mkdir(parents=True, exist_ok=True)
    _preference_path(classroom_dir).write_text(mode_name + "\n")


# ---------------------------------------------------------------------------
# Coordinator fetch — best-effort, falls back to cache or default
# ---------------------------------------------------------------------------


def fetch_policy(
    *,
    coordinator_base_url: str,
    timeout_s: float = 3.0,
) -> ClassroomModePolicy | None:
    """Fetch the classroom's policy from the coordinator. Returns None
    on any error; caller falls back to cache or default."""
    url = coordinator_base_url.rstrip("/") + "/classroom/policy"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            if resp.status != 200:
                return None
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return None
    try:
        raw = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    return ClassroomModePolicy.from_dict(raw)


def resolve_policy(
    *,
    classroom_dir: Path,
    coordinator_base_url: str | None,
) -> ClassroomModePolicy:
    """Best-available policy, in order of trust:

    1. Fresh fetch from the coordinator (if reachable) — save to cache
    2. Local cache
    3. Default (all modes allowed, nothing forced)
    """
    if coordinator_base_url:
        live = fetch_policy(coordinator_base_url=coordinator_base_url)
        if live is not None:
            save_cached_policy(classroom_dir, live)
            return live
    cached = load_cached_policy(classroom_dir)
    if cached is not None:
        return cached
    return ClassroomModePolicy.default()


__all__ = [
    "fetch_policy",
    "load_cached_policy",
    "load_preference",
    "resolve_policy",
    "save_cached_policy",
    "save_preference",
]
