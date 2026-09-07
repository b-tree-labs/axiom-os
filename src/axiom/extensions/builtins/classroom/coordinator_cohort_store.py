# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""File-backed instructor cohort store.

Keeps each classroom's :class:`ClassroomCohort` and its public URL on
disk so ``axi classroom invite`` (mint process) and ``axi classroom
serve`` (long-running) share a consistent roster across restarts.

Layout::

    <base_dir>/classrooms/<classroom_id>/cohort.json

Each file holds::

    {
      "cohort": {<ClassroomCohort fields as JSON>},
      "coordinator_url": "https://test-coordinator.example/classroom/join" | null
    }

The URL is kept alongside the cohort so the instructor only types it
once — subsequent invites for the same classroom pull it from here.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .classroom_federation import ClassroomCohort, CohortMember


class CohortNotFoundError(KeyError):
    """Raised when a classroom has no on-disk cohort record."""


@dataclass
class FileCohortStore:
    """Per-instructor, multi-classroom cohort store."""

    base_dir: Path

    # ---- Path helpers ----

    def _classroom_dir(self, classroom_id: str) -> Path:
        return self.base_dir / "classrooms" / classroom_id

    def _cohort_path(self, classroom_id: str) -> Path:
        return self._classroom_dir(classroom_id) / "cohort.json"

    # ---- Public API ----

    def exists(self, classroom_id: str) -> bool:
        return self._cohort_path(classroom_id).is_file()

    def save(
        self,
        cohort: ClassroomCohort,
        *,
        coordinator_url: str | None = None,
        mode_policy: dict | None = None,
    ) -> None:
        """Write cohort atomically.

        ``coordinator_url`` is preserved when omitted (see
        ``_load_coordinator_url_or_none``). ``mode_policy`` is the
        JSON dict form of :class:`ClassroomModePolicy`; when omitted,
        an existing policy on disk is preserved.
        """
        existing = self._read_or_empty(cohort.classroom_id)
        url_to_write = (
            coordinator_url
            if coordinator_url is not None
            else existing.get("coordinator_url")
        )
        policy_to_write = (
            mode_policy
            if mode_policy is not None
            else existing.get("mode_policy")
        )

        payload = {
            "cohort": _cohort_to_dict(cohort),
            "coordinator_url": url_to_write,
            "mode_policy": policy_to_write,
        }
        self._atomic_write(self._cohort_path(cohort.classroom_id), payload)

    def get_mode_policy(self, classroom_id: str) -> dict | None:
        """Return the raw dict policy, or None if unset."""
        data = self._read(classroom_id)
        pol = data.get("mode_policy")
        return pol if isinstance(pol, dict) else None

    def load(self, classroom_id: str) -> ClassroomCohort:
        data = self._read(classroom_id)
        return _cohort_from_dict(data["cohort"])

    def get_coordinator_url(self, classroom_id: str) -> str | None:
        data = self._read(classroom_id)
        url = data.get("coordinator_url")
        return str(url) if url else None

    def list_ids(self) -> list[str]:
        root = self.base_dir / "classrooms"
        if not root.is_dir():
            return []
        return sorted(
            c.name for c in root.iterdir()
            if c.is_dir() and (c / "cohort.json").is_file()
        )

    # ---- Internals ----

    def _read(self, classroom_id: str) -> dict:
        path = self._cohort_path(classroom_id)
        if not path.is_file():
            raise CohortNotFoundError(
                f"no cohort record for classroom {classroom_id!r}"
            )
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"cohort file for {classroom_id!r} is corrupt: {exc}"
            ) from exc
        if not isinstance(raw, dict) or "cohort" not in raw:
            raise ValueError(
                f"cohort file for {classroom_id!r} is corrupt: missing 'cohort' key"
            )
        return raw

    def _load_coordinator_url_or_none(self, classroom_id: str) -> str | None:
        if not self._cohort_path(classroom_id).is_file():
            return None
        try:
            return self.get_coordinator_url(classroom_id)
        except (CohortNotFoundError, ValueError):
            return None

    def _read_or_empty(self, classroom_id: str) -> dict:
        """Read current on-disk payload for ``classroom_id``, or ``{}`` if
        none/corrupt. Used for merge-style saves that preserve fields
        the caller didn't touch."""
        if not self._cohort_path(classroom_id).is_file():
            return {}
        try:
            return self._read(classroom_id)
        except (CohortNotFoundError, ValueError):
            return {}

    def _atomic_write(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as tf:
            json.dump(payload, tf, indent=2, sort_keys=True)
            tmp_path = Path(tf.name)
        os.replace(tmp_path, path)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _cohort_to_dict(cohort: ClassroomCohort) -> dict:
    return {
        "classroom_id": cohort.classroom_id,
        "coordinator_node": cohort.coordinator_node,
        "created_at": cohort.created_at,
        "members": [asdict(m) for m in cohort.members],
    }


def _cohort_from_dict(raw: dict) -> ClassroomCohort:
    members = [
        CohortMember(
            student_id=str(m["student_id"]),
            member_node=str(m["member_node"]),
            invite_token=str(m["invite_token"]),
            status=str(m.get("status", "ACTIVE")),
            joined_at=_opt(m.get("joined_at")),
            quarantine_reason=_opt(m.get("quarantine_reason")),
            quarantined_at=_opt(m.get("quarantined_at")),
            recovery_approver=_opt(m.get("recovery_approver")),
            recovered_at=_opt(m.get("recovered_at")),
            revoked_reason=_opt(m.get("revoked_reason")),
            revoked_at=_opt(m.get("revoked_at")),
        )
        for m in raw.get("members", [])
    ]
    return ClassroomCohort(
        classroom_id=str(raw["classroom_id"]),
        coordinator_node=str(raw["coordinator_node"]),
        members=members,
        created_at=_opt(raw.get("created_at")),
    )


def _opt(v):
    return str(v) if v is not None else None


__all__ = ["CohortNotFoundError", "FileCohortStore"]
