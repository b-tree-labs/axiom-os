# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Storage-connector registry (ADR-110 §Decision-1, ADR-220 §1 and §4).

``kind`` -> factory over the one shared ``ConnectorRegistry``. Nothing here names
a vendor. A provider is any module that exposes ``register(registry)``, which
calls ``registry.register(kind, factory)``. The platform's own providers are the
sub-packages of this package that expose it; an extension ships one through the
``axiom.storage_providers`` entry-point group. Either way a new vendor is a
package and never an edit to this file.

Loading is execution, so discovery honours the portfolio gate (ADR-023-A1
A1.3): an entry point from a distribution that is not a portfolio member is
skipped and logged, never imported. Discovery runs once per process and again on
:func:`rediscover` (what a connector reload calls, so a newly installed provider
is picked up without a restart).
"""

from __future__ import annotations

import logging
import threading
from importlib.metadata import entry_points
from typing import Any

from axiom.infra.connector_registry import ConnectorRegistry

log = logging.getLogger(__name__)

#: The entry-point group a storage provider ships under.
PROVIDER_GROUP = "axiom.storage_providers"

_STORES: ConnectorRegistry = ConnectorRegistry("storage connector")
_lock = threading.Lock()
_discovered = False
_loaded: list[str] = []


def register_storage_connector(kind: str, factory: Any, *, replace: bool = False) -> None:
    _STORES.register(kind, factory, replace=replace)


class _Registrar:
    """What an entry point is handed: ``register(kind, factory)`` and nothing else."""

    def __init__(self, replace: bool) -> None:
        self._replace = replace

    def register(self, kind: str, factory: Any) -> None:
        register_storage_connector(kind, factory, replace=self._replace)


def _portfolio() -> frozenset[str]:
    from axiom.extensions.builtins.data_platform.conformance.discovery import (
        portfolio_distributions,
    )

    return portfolio_distributions()


def _canonical(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


#: Modules of this package that are the seam itself, not providers.
_SEAM = frozenset({"protocol", "registry", "manager", "roles"})


def _builtins(*, replace: bool) -> list[str]:
    """The platform's own providers: sub-modules of this package with ``register``."""
    import importlib
    import pkgutil

    from . import __path__ as here

    loaded = []
    for info in sorted(pkgutil.iter_modules(here), key=lambda m: m.name):
        if info.name in _SEAM or info.name.startswith("_") or info.name == "tests":
            continue
        try:
            module = importlib.import_module(f"{__package__}.{info.name}")
            hook = getattr(module, "register", None)
            if callable(hook):
                hook(_Registrar(replace=replace))
                loaded.append(info.name)
        except Exception as exc:  # noqa: BLE001 - one bad provider must not stop the rest
            log.warning("built-in storage provider %r failed to register: %s", info.name, exc)
    return loaded


def discover(*, force: bool = False, allow: frozenset[str] | None = None) -> list[str]:
    """Register every portfolio-supplied provider. Returns the kinds' entry-point names loaded."""
    global _discovered
    with _lock:
        if _discovered and not force:
            return list(_loaded)
        allowed = _portfolio() if allow is None else frozenset(map(_canonical, allow))
        try:
            eps = list(entry_points(group=PROVIDER_GROUP))
        except Exception as exc:  # noqa: BLE001 - discovery never takes the process down
            log.warning("storage provider entry-points lookup failed: %s", exc)
            eps = []
        loaded: list[str] = _builtins(replace=force)
        for ep in eps:
            dist = _canonical(getattr(getattr(ep, "dist", None), "name", "") or "")
            if not dist or dist not in allowed:
                log.warning(
                    "refusing storage provider %r from %r: not a portfolio distribution "
                    "(ADR-023-A1 A1.3)",
                    ep.name,
                    dist or "<unknown>",
                )
                continue
            try:
                ep.load()(_Registrar(replace=force))
            except Exception as exc:  # noqa: BLE001 - one bad package must not stop the rest
                log.warning(
                    "storage provider %r from %r failed to register: %s", ep.name, dist, exc
                )
                continue
            loaded.append(ep.name)
        _loaded[:] = loaded
        _discovered = True
        return list(loaded)


def rediscover() -> list[str]:
    """Discover again (a reload): newly installed providers register, existing ones refresh."""
    return discover(force=True)


def available_storage_connectors() -> tuple[str, ...]:
    discover()
    return _STORES.available()


def get_storage_connector(kind: str, **config: Any):
    discover()
    return _STORES.create(kind, **config)


__all__ = [
    "PROVIDER_GROUP",
    "available_storage_connectors",
    "discover",
    "get_storage_connector",
    "rediscover",
    "register_storage_connector",
]
