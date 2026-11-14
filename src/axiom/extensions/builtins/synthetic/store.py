# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The journey store: smoke verdicts kept by commit (LOOP-S6).

One JSON file per commit under ``<state>/synthetic/smoke/``, appended to on every run.
The newest run is the verdict, so a later failure supersedes an earlier pass. The
release cut reads it beside the CI job's own verdict for the same commit.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
#: Kept per commit; older runs fall off so a file stays small.
MAX_RUNS = 20


def results_dir(state_dir: str | Path) -> Path:
    return Path(state_dir) / "synthetic" / "smoke"


def _path(state_dir: str | Path, commit: str) -> Path:
    if not _COMMIT.match(commit or ""):
        raise ValueError(f"{commit!r} is not a commit id")
    return results_dir(state_dir) / f"{commit}.json"


def runs(state_dir: str | Path, commit: str) -> list[dict[str, Any]]:
    """Every recorded run for ``commit``, oldest first."""
    try:
        data = json.loads(_path(state_dir, commit).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in data.get("runs") or [] if isinstance(r, dict)]


def record(state_dir: str | Path, *, commit: str, result: dict[str, Any], source: str = "local") -> dict[str, Any]:
    """Append one run's result for ``commit``; returns what was written."""
    path = _path(state_dir, commit)
    entry = {
        "commit": commit,
        "status": result.get("status", "unproven"),
        "role": result.get("role", ""),
        "base_url": result.get("base_url", ""),
        "failing": list(result.get("failing") or []),
        "source": source,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    kept = [*runs(state_dir, commit), entry][-MAX_RUNS:]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{commit}.{uuid.uuid4().hex}.tmp"
    tmp.write_text(json.dumps({"commit": commit, "runs": kept}, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return entry


def verdict(state_dir: str | Path, commit: str) -> dict[str, Any] | None:
    """The newest run for ``commit``, or ``None`` when nothing was recorded."""
    found = runs(state_dir, commit)
    return found[-1] if found else None


__all__ = ["MAX_RUNS", "record", "results_dir", "runs", "verdict"]
