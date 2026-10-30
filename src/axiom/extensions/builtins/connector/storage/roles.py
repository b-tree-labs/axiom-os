# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Storage roles: what a feature asks for instead of a vendor (ADR-220 §6).

A feature names the ROLE it needs (``library``, ``backup.offsite``, ...) and
declares what happens when no connector is mapped to it:

* ``default``: the always-mounted ``default`` connector serves it, and
  ``note_on_default`` (if any) is what status says is missing;
* ``absent``: the feature is hidden. It gets no store, no error and no empty
  vendor menu.

Configuration maps a role to a connector by name, in
``<state>/connector/storage-roles.toml``::

    [roles]
    "backup.offsite" = "team-share"
"""

from __future__ import annotations

import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT = "default"
ABSENT = "absent"

#: The connector every install has, with no configuration.
DEFAULT_CONNECTOR = "default"


@dataclass(frozen=True)
class StorageRole:
    name: str
    fallback: str = DEFAULT
    description: str = ""
    #: What status says when the default store serves this role.
    note_on_default: str = ""

    def __post_init__(self) -> None:
        if self.fallback not in (DEFAULT, ABSENT):
            raise ValueError(f"role {self.name!r}: fallback must be {DEFAULT!r} or {ABSENT!r}")


_ROLES: dict[str, StorageRole] = {}
_lock = threading.Lock()


def declare_role(role: StorageRole) -> StorageRole:
    """Declare a role. Idempotent for the same declaration; loud on a different one."""
    with _lock:
        have = _ROLES.get(role.name)
        if have is not None and have != role:
            raise ValueError(f"storage role {role.name!r} is already declared differently: {have}")
        _ROLES[role.name] = role
    return role


def declared_role(name: str) -> StorageRole:
    try:
        return _ROLES[name]
    except KeyError:
        known = ", ".join(sorted(_ROLES)) or "(none)"
        raise KeyError(f"storage role {name!r} is not declared; known: {known}") from None


def declared_roles() -> list[StorageRole]:
    return [_ROLES[k] for k in sorted(_ROLES)]


def roles_path(state_dir: Path | None = None) -> Path:
    from axiom.infra.paths import get_user_state_dir

    return (state_dir or get_user_state_dir()) / "connector" / "storage-roles.toml"


def load_role_map(state_dir: Path | None = None) -> dict[str, str]:
    """``role -> connector name`` from configuration; empty when nothing is mapped."""
    path = roles_path(state_dir)
    if not path.is_file():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in (data.get("roles") or {}).items() if str(v).strip()}


# The platform's own storage-using features. A feature that needs another role
# declares it beside its own code.
for _role in (
    StorageRole("library", description="uploaded files and the Library"),
    StorageRole("exports", description="exported tables, charts and documents"),
    StorageRole("archive", description="data moved out of the live tiers"),
    StorageRole("backup", description="database backups (the first copy)"),
    StorageRole(
        "backup.offsite",
        description="a second copy of each backup, somewhere other than this machine",
        note_on_default="on this machine only: add an off-site provider",
    ),
    StorageRole(
        "deposits.inbound",
        fallback=ABSENT,
        description="a shared folder partners drop files into (partners can also push to "
        "the ingest front door, which needs no folder)",
    ),
):
    declare_role(_role)


__all__ = [
    "ABSENT",
    "DEFAULT",
    "DEFAULT_CONNECTOR",
    "StorageRole",
    "declare_role",
    "declared_role",
    "declared_roles",
    "load_role_map",
    "roles_path",
]
