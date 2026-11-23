# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Append-only audit log for TIDY storage hygiene.

Never write secrets, tokens, or raw environment values. The log is a JSONL
file under the agent's state directory so an operator can replay what TIDY
proposed or deleted.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_NAME = "storage-audit.jsonl"


def audit_path(state_dir: Path) -> Path:
    return Path(state_dir) / "agents" / "tidy" / LOG_NAME


def append_audit(state_dir: Path, event: dict[str, Any], *, now: datetime | None = None) -> Path:
    """Append one event. ``event`` must not contain secrets."""
    now = now or datetime.now(timezone.utc)
    path = audit_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": now.isoformat(), **event}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
    return path
