# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The sites this node serves as tenants, declared as configuration (MVP-10-A2).

Adding a tenant used to mean editing a comma list in the environment
(``AXIOM_SERVED_SITES``) and, for a display name or a former id, nothing at all.
This is the registry as a file a deployment owns::

    # $AXIOM_TENANTS_FILE
    [[tenant]]
    id = "site-a"
    name = "Site A"
    also_known_as = ["site-a-old"]

    [[tenant]]
    id = "site-b"
    name = "Site B"

Adding a tenant is adding a block. Nothing is restarted and no code changes: the
file is re-read when it changes, :func:`axiom.infra.site_scope.deployment_sites`
serves its ids beside ``AXIOM_SERVED_SITES``, its former ids resolve to the one
site (:mod:`axiom.infra.site_identity`), and its name is what the scope selector
shows.

The registry says what is served. Who may read which tenant is still the
credential's site and the one decision function, never this file.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from axiom.infra.toml_compat import tomllib

TENANTS_FILE_ENV = "AXIOM_TENANTS_FILE"


class TenantsFileError(ValueError):
    """The tenants file cannot be used; it is reported, and serves no tenants."""


@dataclass(frozen=True)
class Tenant:
    id: str
    name: str = ""
    also_known_as: tuple[str, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        return self.name or self.id

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.label, "also_known_as": list(self.also_known_as)}


_LOCK = threading.Lock()
_CACHE: dict[str, tuple[tuple[int, int, int], tuple[Tenant, ...]]] = {}


def tenants_path(env: Mapping[str, str] | None = None) -> Path | None:
    raw = ((env if env is not None else os.environ).get(TENANTS_FILE_ENV) or "").strip()
    return Path(os.path.expanduser(raw)) if raw else None


def parse(text: str, *, where: str = "tenants file") -> tuple[Tenant, ...]:
    """The tenants a file declares, or raise :class:`TenantsFileError`."""
    from axiom.webauth.users import validate_site

    try:
        data = tomllib.loads(text)
    except Exception as exc:  # noqa: BLE001 - any parse failure is the file's problem
        raise TenantsFileError(f"{where}: not valid TOML: {exc}") from exc
    rows = data.get("tenant") or []
    if not isinstance(rows, list):
        raise TenantsFileError(f"{where}: [[tenant]] must be an array of tables")
    out: list[Tenant] = []
    seen: set[str] = set()
    for i, row in enumerate(rows):
        at = f"{where}: tenant {i + 1}"
        if not isinstance(row, dict):
            raise TenantsFileError(f"{at}: must be a table")
        try:
            tid = validate_site(row.get("id"))
        except ValueError as exc:
            raise TenantsFileError(f"{at}: {exc}") from exc
        if not tid:
            raise TenantsFileError(f"{at}: 'id' is required")
        if tid in seen:
            raise TenantsFileError(f"{at}: duplicate id {tid!r}")
        seen.add(tid)
        olds = row.get("also_known_as") or ()
        if isinstance(olds, str) or not isinstance(olds, (list, tuple)):
            raise TenantsFileError(f"{at}: 'also_known_as' must be a list of ids")
        out.append(Tenant(id=tid, name=str(row.get("name") or "").strip(),
                          also_known_as=tuple(str(o).strip() for o in olds if str(o).strip())))
    return tuple(out)


def load(env: Mapping[str, str] | None = None) -> tuple[Tenant, ...]:
    """The declared tenants, re-read when the file changes; ``()`` when none.

    A file that cannot be read or parsed serves no tenants from the registry (the
    environment's list still stands) and says why once per state of the file.
    """
    path = tenants_path(env)
    if path is None:
        return ()
    try:
        st = path.stat()
    except OSError:
        return ()
    sig = (st.st_mtime_ns, st.st_ino, st.st_size)
    key = str(path)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached[0] == sig:
            return cached[1]
    try:
        found = parse(path.read_text(encoding="utf-8"), where=str(path))
    except (OSError, TenantsFileError) as exc:
        import logging

        logging.getLogger(__name__).warning("tenants file serves no tenants: %s", exc)
        found = ()
    _declare_aliases(found)
    with _LOCK:
        _CACHE[key] = (sig, found)
    return found


def _declare_aliases(found: tuple[Tenant, ...]) -> None:
    from axiom.infra import site_identity

    for t in found:
        try:
            site_identity.declare(t.id, also_known_as=t.also_known_as)
        except ValueError as exc:
            import logging

            logging.getLogger(__name__).warning("tenant %s: %s", t.id, exc)


def ids(env: Mapping[str, str] | None = None) -> frozenset[str]:
    return frozenset(t.id for t in load(env))


def label(site: str, env: Mapping[str, str] | None = None) -> str:
    """The display name of ``site``, else the id itself."""
    for t in load(env):
        if t.id == site:
            return t.label
    return site


def _reset_for_tests() -> None:
    with _LOCK:
        _CACHE.clear()


__all__ = ["TENANTS_FILE_ENV", "Tenant", "TenantsFileError", "ids", "label", "load", "parse",
           "tenants_path"]
