# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Persisted state — which harnesses have shims, where they were emitted.

`axi update` consumes this to know what to regenerate after self-update so
shims don't go stale when extensions add/remove verbs.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

STATE_FILENAME = "commands_state.json"


@dataclass
class GeneratedHarness:
    harness: str  # "claude" | "cursor" | "codex" | "vscode" | "opencode" | "neovim" | "vim"
    out_dir: str  # absolute path to the directory shims were rooted at
    file_count: int
    last_generated: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )


def _state_path(state_dir: Path) -> Path:
    return state_dir / "agents" / "commands" / STATE_FILENAME


def load(state_dir: Path) -> list[GeneratedHarness]:
    p = _state_path(state_dir)
    if not p.exists():
        return []
    raw = json.loads(p.read_text(encoding="utf-8") or "[]")
    return [GeneratedHarness(**r) for r in raw]


def save(state_dir: Path, entries: list[GeneratedHarness]) -> None:
    p = _state_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps([asdict(e) for e in entries], indent=2) + "\n",
        encoding="utf-8",
    )


def upsert(
    state_dir: Path,
    harness: str,
    out_dir: Path,
    file_count: int,
) -> GeneratedHarness:
    entries = load(state_dir)
    entry = GeneratedHarness(
        harness=harness, out_dir=str(out_dir), file_count=file_count
    )
    entries = [e for e in entries if e.harness != harness]
    entries.append(entry)
    save(state_dir, entries)
    return entry
