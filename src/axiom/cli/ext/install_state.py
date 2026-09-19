# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Install-state tracking for ``axi``-managed extensions.

State lives at ``$AXIOM_HOME/state.json`` (default
``~/.axiom/state.json``). Every mutation lands via atomic write
(tempfile + ``os.replace``); readers never observe a partially-written
blob. The six Phase 4 verbs (``list``, ``install``, ``uninstall``,
``update``, ``search``, ``show``) consume this state.

Schema v1::

    {
      "schema_version": 1,
      "installed": {
        "<name>": {
          "version": "X.Y.Z",
          "installed_at": "<ISO8601>",
          "install_path": "<absolute path to unpacked dir>",
          "artifact_sha256": "<hex>",
          "signature_sha256": "<hex of the .sig bytes>",
          "registry_url": "file:///.../registry"
        }
      }
    }

Only one record per name — installing a newer version replaces the old
record. Callers that want history query the registry backend instead.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from axiom.cli.ext.commands.config import _axiom_home

_STATE_SCHEMA_VERSION = 1
_STATE_FILENAME = "state.json"


def state_path() -> Path:
    """Return the absolute path to ``$AXIOM_HOME/state.json``."""
    return _axiom_home() / _STATE_FILENAME


@dataclass(frozen=True)
class InstallRecord:
    """One row of the ``installed`` table.

    All fields are required. ``install_path`` is always absolute; the
    caller that constructs the record is responsible for resolving
    relative paths before passing them in.
    """

    name: str
    version: str
    installed_at: str
    install_path: str
    artifact_sha256: str
    signature_sha256: str
    registry_url: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, name: str, data: dict[str, Any]) -> InstallRecord:
        return cls(
            name=name,
            version=str(data.get("version", "")),
            installed_at=str(data.get("installed_at", "")),
            install_path=str(data.get("install_path", "")),
            artifact_sha256=str(data.get("artifact_sha256", "")),
            signature_sha256=str(data.get("signature_sha256", "")),
            registry_url=str(data.get("registry_url", "")),
        )


def _seed_state() -> dict[str, Any]:
    return {"schema_version": _STATE_SCHEMA_VERSION, "installed": {}}


def read_state() -> dict[str, Any]:
    """Return the parsed state dict (or the seed if the file is missing).

    A corrupt state file is treated like "no state" for *read* purposes;
    the next :func:`write_state` overwrites it atomically. An unsupported
    schema version, on the other hand, raises :class:`ValueError` so we
    never silently drop records from a newer release.
    """
    path = state_path()
    if not path.exists():
        return _seed_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _seed_state()
    if not isinstance(data, dict) or "installed" not in data:
        return _seed_state()
    schema = data.get("schema_version")
    if schema != _STATE_SCHEMA_VERSION:
        raise ValueError(
            f"$AXIOM_HOME/{_STATE_FILENAME} has unsupported schema_version "
            f"{schema!r}; expected {_STATE_SCHEMA_VERSION}"
        )
    return data


def write_state(data: dict[str, Any]) -> None:
    """Atomically overwrite the state file (tempfile + rename)."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = f"{_STATE_FILENAME}.{uuid.uuid4().hex}.tmp"
    tmp_path = path.parent / tmp_name
    try:
        tmp_path.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def list_installed() -> list[InstallRecord]:
    """Return all install records, sorted by extension name."""
    state = read_state()
    installed = state.get("installed", {}) or {}
    records: list[InstallRecord] = []
    for name in sorted(installed.keys()):
        entry = installed[name]
        if not isinstance(entry, dict):
            continue
        records.append(InstallRecord.from_json(name, entry))
    return records


def get_installed(name: str) -> InstallRecord | None:
    """Return the install record for ``name`` or ``None``."""
    state = read_state()
    entry = (state.get("installed") or {}).get(name)
    if not isinstance(entry, dict):
        return None
    return InstallRecord.from_json(name, entry)


def record_install(record: InstallRecord) -> None:
    """Upsert ``record``. Exactly one record per extension name."""
    state = read_state()
    installed = state.setdefault("installed", {})
    payload = record.to_json()
    # The name is the key — drop it from the value to avoid duplication.
    payload.pop("name", None)
    installed[record.name] = payload
    state["schema_version"] = _STATE_SCHEMA_VERSION
    write_state(state)


def drop_install(name: str) -> InstallRecord | None:
    """Remove the record for ``name`` (if any) and return what was dropped."""
    state = read_state()
    installed = state.get("installed") or {}
    entry = installed.pop(name, None)
    if entry is None:
        return None
    state["installed"] = installed
    state["schema_version"] = _STATE_SCHEMA_VERSION
    write_state(state)
    return InstallRecord.from_json(name, entry) if isinstance(entry, dict) else None


__all__ = [
    "InstallRecord",
    "drop_install",
    "get_installed",
    "list_installed",
    "read_state",
    "record_install",
    "state_path",
    "write_state",
]
