# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The base-drift repairs in flight, one per drift signature.

A drift signature is the catalog id, the base branch, the working
directory and the versions the repair moves to. One pull request carries
the repair for a signature at a time; every other pull request red with
the same signature waits for it, and once the carrier lands brings the
base in and re-runs.

Statuses: ``verifying`` (the repair is running), ``pushed`` (committed to
the carrier and pushed), ``landed`` (the carrier merged), ``failed``
(verify failed, or the carrier closed without merging). ``attempts``
counts repairs tried for the signature, which the try cap reads.

Stored like the lander's other state: one JSON file under
``<state>/agents/rivet/``, replaced atomically, with an advisory lock
around each read-modify-write so two passes cannot interleave.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

ACTIVE = ("verifying", "pushed")
#: A landed or failed entry is kept this long, so a late pass still sees it.
KEEP_FOR = timedelta(days=14)


def ledger_path(state_dir: Path) -> Path:
    return Path(state_dir) / "agents" / "rivet" / "drift-repairs.json"


class PullStates(Protocol):
    def pr_state(self, number: int) -> dict: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


class DriftLedger:
    def __init__(self, state_dir: Path) -> None:
        self.path = ledger_path(state_dir)

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, sort_keys=True, indent=1), encoding="utf-8")
        os.replace(temporary, self.path)

    @contextmanager
    def _locked(self) -> Iterator[dict[str, dict[str, Any]]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(".lock"), "a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                data = self._read()
                yield data
                self._write(data)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def get(self, key: str) -> dict[str, Any] | None:
        entry = self._read().get(key)
        return dict(entry) if isinstance(entry, dict) else None

    def for_pr(self, pr: int | str) -> dict[str, Any] | None:
        """The newest entry a pull request carries."""
        found = [
            dict(entry, key=key)
            for key, entry in self._read().items()
            if isinstance(entry, dict) and str(entry.get("pr")) == str(pr)
        ]
        found.sort(key=lambda entry: str(entry.get("updated_at") or ""))
        return found[-1] if found else None

    def in_flight(self) -> list[dict[str, Any]]:
        """Active repairs as ``{key, pr}``, the shape ``plan_repair`` reads."""
        return [
            {"key": key, "pr": str(entry.get("pr") or "")}
            for key, entry in self._read().items()
            if isinstance(entry, dict) and entry.get("status") in ACTIVE
        ]

    def upsert(self, key: str, **fields: Any) -> dict[str, Any]:
        with self._locked() as data:
            entry = dict(data.get(key) or {})
            if not entry:
                entry["created_at"] = _now()
                entry["attempts"] = 0
            entry.update({name: value for name, value in fields.items() if value is not None})
            entry["updated_at"] = _now()
            data[key] = entry
            return dict(entry)

    def note_attempt(self, key: str, pr: str) -> dict[str, Any]:
        """Start a repair: the signature is now carried by ``pr`` and verifying."""
        with self._locked() as data:
            entry = dict(data.get(key) or {"created_at": _now(), "attempts": 0})
            entry.update(
                {
                    "pr": str(pr),
                    "status": "verifying",
                    "attempts": int(entry.get("attempts") or 0) + 1,
                    "updated_at": _now(),
                }
            )
            entry.pop("commit", None)
            data[key] = entry
            return dict(entry)

    def tell_once(self, key: str, pr: int | str) -> bool:
        """True the first time ``pr`` is told it waits on ``key``'s repair."""
        with self._locked() as data:
            entry = dict(data.get(key) or {"created_at": _now(), "attempts": 0})
            told = [str(item) for item in entry.get("told") or []]
            if str(pr) in told:
                return False
            entry["told"] = [*told, str(pr)]
            data[key] = entry
            return True

    def sync(self, host: PullStates) -> list[str]:
        """Mark carriers that merged ``landed`` and that closed ``failed``; drop old entries."""
        changed: list[str] = []
        with self._locked() as data:
            cutoff = datetime.now(UTC) - KEEP_FOR
            for key in list(data):
                entry = data[key]
                if not isinstance(entry, dict):
                    data.pop(key)
                    continue
                if entry.get("status") in ACTIVE and str(entry.get("pr") or "").isdigit():
                    try:
                        state = str(host.pr_state(int(entry["pr"])).get("state") or "").lower()
                    except Exception:
                        continue
                    if state == "merged":
                        entry.update({"status": "landed", "landed_at": _now(), "updated_at": _now()})
                        changed.append(key)
                    elif state == "closed":
                        entry.update({"status": "failed", "reason": "carrier closed", "updated_at": _now()})
                        changed.append(key)
                elif entry.get("status") not in ACTIVE:
                    try:
                        stamp = datetime.fromisoformat(str(entry.get("updated_at")))
                    except ValueError:
                        continue
                    if stamp < cutoff:
                        data.pop(key)
        return changed


__all__ = ["ACTIVE", "DriftLedger", "ledger_path"]
