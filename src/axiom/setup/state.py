# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Setup progress persistence for axi config.

Saves wizard progress to .neut/setup-state.json so users can resume
an interrupted session. State older than 30 days is discarded as stale.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.state import atomic_write

# Stale threshold in seconds (30 days)
_STALE_SECONDS = 30 * 24 * 60 * 60


def _find_project_root() -> Path:
    """Find the project root (same strategy as publisher/config.py)."""
    env_root = os.environ.get("AXIOM_ROOT")
    if env_root:
        return Path(env_root).resolve()

    path = Path(__file__).resolve().parent
    while path != path.parent:
        if (path / ".git").exists():
            return path
        path = path.parent

    return Path.cwd()


def _state_path(root: Path | None = None) -> Path:
    """Return the path to setup-state.json."""
    if root is None:
        root = _find_project_root()
    neut_dir = root / ".neut"
    neut_dir.mkdir(parents=True, exist_ok=True)
    return neut_dir / "setup-state.json"


@dataclass
class SetupState:  # pylint: disable=too-many-instance-attributes
    """Tracks wizard progress across sessions."""

    current_phase: str = "probe"
    completed_phases: list[str] = field(default_factory=list)
    probe_result: dict[str, Any] = field(default_factory=dict)
    infra_configured: bool = False
    credentials_configured: dict[str, bool] = field(default_factory=dict)
    config_files_created: dict[str, bool] = field(default_factory=dict)
    test_results: dict[str, str] = field(default_factory=dict)
    user_choices: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    updated_at: str = ""

    def mark_phase_complete(self, phase: str) -> None:
        """Mark a phase as completed and advance current_phase."""
        if phase not in self.completed_phases:
            self.completed_phases.append(phase)
        self.updated_at = datetime.now(UTC).isoformat()

    def is_phase_complete(self, phase: str) -> bool:
        """Return True if the given phase has been completed."""
        return phase in self.completed_phases

    def is_stale(self) -> bool:
        """Return True if the state is older than 30 days."""
        try:
            created = datetime.fromisoformat(self.created_at)
            now = datetime.now(UTC)
            return (now - created).total_seconds() > _STALE_SECONDS
        except (ValueError, TypeError):
            return True

    def to_dict(self) -> dict[str, Any]:
        """Serialize state to a JSON-compatible dictionary."""
        return {
            "current_phase": self.current_phase,
            "completed_phases": self.completed_phases,
            "probe_result": self.probe_result,
            "credentials_configured": self.credentials_configured,
            "config_files_created": self.config_files_created,
            "test_results": self.test_results,
            "user_choices": self.user_choices,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SetupState:
        """Deserialize state from a dictionary (e.g. parsed JSON)."""
        return cls(
            current_phase=d.get("current_phase", "probe"),
            completed_phases=d.get("completed_phases", []),
            probe_result=d.get("probe_result", {}),
            credentials_configured=d.get("credentials_configured", {}),
            config_files_created=d.get("config_files_created", {}),
            test_results=d.get("test_results", {}),
            user_choices=d.get("user_choices", {}),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )


def save_state(state: SetupState, root: Path | None = None) -> Path:
    """Persist setup state to disk."""
    state.updated_at = datetime.now(UTC).isoformat()
    path = _state_path(root)
    atomic_write(path, state.to_dict())
    return path


def load_state(root: Path | None = None) -> SetupState | None:
    """Load setup state from disk. Returns None if no state or stale."""
    path = _state_path(root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        state = SetupState.from_dict(data)
        if state.is_stale():
            clear_state(root)
            return None
        return state
    except (json.JSONDecodeError, KeyError):
        return None


def clear_state(root: Path | None = None) -> None:
    """Remove persisted setup state."""
    path = _state_path(root)
    if path.exists():
        path.unlink()
