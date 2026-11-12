# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Configured connectors, from every family that reports them (ADR-220 §3, ADR-238 D5).

A family is a status provider, ``rows(state_dir) -> [{connector, kind, health, detail}]``,
and may also answer ``connector reload`` with ``reload(state_dir, name) -> [rows]``
(``name`` is ``None`` for all; a family returns no row for a name it does not have):

* a builtin extension's ``connector_status`` module, found by convention, so a
  builtin needs no entry-point metadata and nothing is reinstalled;
* an ``axiom.connector_status`` entry point, loaded only from a portfolio
  distribution (ADR-023-A1 A1.3: loading is execution).

This module names no kind (ADR-220 §1). A provider that fails is reported and hides
nothing else.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: The entry-point group an external family ships its provider under.
PROVIDER_GROUP = "axiom.connector_status"
#: The entry-point group an external family ships its reloader under.
RELOAD_GROUP = "axiom.connector_reload"
#: Where builtin extensions live, and the module and function a builtin provider is.
BUILTINS_PACKAGE = "axiom.extensions.builtins"
MODULE = "connector_status"
FUNCTION = "rows"
RELOAD_FUNCTION = "reload"
#: ADR-220 §3.
HEALTH = ("ready", "degraded", "failed", "unmounted")

Provider = Callable[[Path], list[dict[str, Any]]]
Reloader = Callable[[Path, str | None], list[dict[str, Any]]]


def _canonical(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def _portfolio() -> frozenset[str]:
    from axiom.extensions.builtins.data_platform.conformance.discovery import (
        portfolio_distributions,
    )

    return portfolio_distributions()


def _entry_points() -> list[Any]:
    from importlib.metadata import entry_points

    try:
        return list(entry_points(group=PROVIDER_GROUP))
    except Exception as exc:  # noqa: BLE001 - discovery never takes status down
        log.warning("connector status providers: entry-point lookup failed: %s", exc)
        return []


def _builtins(function: str = FUNCTION, *, required: bool = True) -> list[tuple[str, Any]]:
    """Builtins with a ``connector_status`` module. Only those are imported.

    ``required=False`` is for a hook a family may leave out (``reload``): its
    absence is not a failure to report.
    """
    package = importlib.import_module(BUILTINS_PACKAGE)
    out: list[tuple[str, Any]] = []
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        if not info.ispkg or not any(
            (Path(p) / info.name / f"{MODULE}.py").exists() for p in package.__path__
        ):
            continue
        try:
            module = importlib.import_module(f"{BUILTINS_PACKAGE}.{info.name}.{MODULE}")
            if not required and not hasattr(module, function):
                continue
            fn = getattr(module, function)
        except Exception as exc:  # noqa: BLE001 - one bad family must not hide the rest
            log.warning("connector %s hook %r failed to load: %s", function, info.name, exc)
            continue
        out.append((info.name, fn))
    return out


def _gated(eps: list[Any], allow: frozenset[str] | None, what: str) -> list[tuple[str, Any]]:
    """Load the entry points of portfolio distributions only (ADR-023-A1 A1.3: loading is execution)."""
    if not eps:
        return []
    allowed = _portfolio() if allow is None else frozenset(map(_canonical, allow))
    found: list[tuple[str, Any]] = []
    for ep in eps:
        dist = _canonical(getattr(getattr(ep, "dist", None), "name", "") or "")
        if not dist or dist not in allowed:
            log.warning(
                "refusing %s %r from %r: not a portfolio distribution (ADR-023-A1 A1.3)",
                what,
                ep.name,
                dist or "<unknown>",
            )
            continue
        try:
            found.append((ep.name, ep.load()))
        except Exception as exc:  # noqa: BLE001
            log.warning("%s %r from %r failed to load: %s", what, ep.name, dist, exc)
    return found


def providers(*, allow: frozenset[str] | None = None) -> list[tuple[str, Provider]]:
    """``(family, rows)`` for every builtin provider and every portfolio entry point."""
    return _builtins() + _gated(_entry_points(), allow, "connector status provider")


def configured_connectors(
    state_dir: Path, *, providers: list[tuple[str, Provider]] | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """``(rows sorted by connector, problems)``; each row carries the family that reported it."""
    # The module attribute, read at call time: a parameter of the same name shadows it here.
    found = providers if providers is not None else sys.modules[__name__].providers()
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    for family, fn in found:
        try:
            reported = list(fn(Path(state_dir)) or [])
        except Exception as exc:  # noqa: BLE001 - isolated to its family
            problems.append(f"{family}: {type(exc).__name__}: {exc}")
            continue
        for row in reported:
            health = row.get("health")
            if health not in HEALTH:
                problems.append(
                    f"{family}: {row.get('connector')!r} reports health {health!r}, not one of {', '.join(HEALTH)}"
                )
                continue
            rows.append({**row, "family": family})
    return sorted(rows, key=lambda r: str(r.get("connector"))), problems


def _reload_entry_points() -> list[Any]:
    from importlib.metadata import entry_points

    try:
        return list(entry_points(group=RELOAD_GROUP))
    except Exception as exc:  # noqa: BLE001 - discovery never takes reload down
        log.warning("connector reloaders: entry-point lookup failed: %s", exc)
        return []


def reloaders(*, allow: frozenset[str] | None = None) -> list[tuple[str, Reloader]]:
    """``(family, reload)`` for every builtin with one and every portfolio entry point."""
    return _builtins(RELOAD_FUNCTION, required=False) + _gated(
        _reload_entry_points(), allow, "connector reloader"
    )


def reload_configured(
    state_dir: Path,
    name: str | None = None,
    *,
    reloaders: list[tuple[str, Reloader]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """``(rows sorted by connector, problems)`` from every family's reloader; names no kind."""
    found = reloaders if reloaders is not None else sys.modules[__name__].reloaders()
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    for family, fn in found:
        try:
            reported = list(fn(Path(state_dir), name) or [])
        except Exception as exc:  # noqa: BLE001 - isolated to its family
            problems.append(f"{family}: {type(exc).__name__}: {exc}")
            continue
        for row in reported:
            health = row.get("health")
            if health not in HEALTH:
                problems.append(
                    f"{family}: {row.get('connector')!r} reports health {health!r}, not one of {', '.join(HEALTH)}"
                )
                continue
            rows.append({**row, "family": family})
    return sorted(rows, key=lambda r: str(r.get("connector"))), problems


def render(rows: list[dict[str, Any]]) -> list[str]:
    """The table every CLI that shows configured connectors prints, one source for all of them."""
    from axiom.infra.cli_format import Column, table, terminal_width

    cols = [Column("health"), Column("connector"), Column("kind"), Column("detail", wrap=True)]
    data = [(r["health"], r["connector"], r["kind"], r.get("detail") or "") for r in rows]
    return [
        f"{len(rows)} configured connector(s):",
        *table(data, cols, width=terminal_width(reserve=2), headers=True),
    ]


__all__ = [
    "BUILTINS_PACKAGE",
    "FUNCTION",
    "HEALTH",
    "MODULE",
    "PROVIDER_GROUP",
    "RELOAD_FUNCTION",
    "RELOAD_GROUP",
    "configured_connectors",
    "providers",
    "reload_configured",
    "reloaders",
    "render",
]
