# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Landing-table discovery — each package declares the tables IT lands into.

The mechanics of "can this table refuse a duplicate" are domain-agnostic and
live next door. The *list of tables* is not: `silver.signals` is the platform's,
a consumer's own landing table belongs to that consumer's package, and a
different tenant lands somewhere else entirely. A hardcoded registry would make
this guard useless to everyone but us, and would put a consumer's table name in
domain-agnostic code — the leak the public-mirror guard exists to catch.

So this mirrors :mod:`..conformance.discovery` exactly, including its security
posture: the ``axiom.data_platform.landing_tables`` group is honoured **only**
for distributions that also declare ``axiom.portfolio_member``. An entry point
is a callable that gets imported, and importing is already execution.

A package declares its tables in ``pyproject.toml``::

    [project.entry-points."axiom.data_platform.landing_tables"]
    my_domain_pack = "my_domain_pack.data_platform:landing_tables"

where ``landing_tables()`` returns ``[("public", "my_measurements")]``.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

from .conformance_gate import PORTFOLIO_GROUP, canonical

log = logging.getLogger(__name__)

#: Entry-point group a platform package uses to declare its landing tables.
#: The value is a zero-argument callable returning ``(schema, table)`` pairs.
LANDING_GROUP = "axiom.data_platform.landing_tables"


def discover_landing_tables(
    *, allow: frozenset[str] | None = None
) -> tuple[list[tuple[str, str]], list[str]]:
    """Every platform-declared landing table, plus the distributions that gave them.

    Returns ``(tables, distributions)``. The second value exists so a caller can
    assert on what it actually loaded: an audit that silently discovered nothing
    is indistinguishable from an audit where everything passed, which is the
    precise failure this module was written to prevent.
    """
    allowed = _portfolio() if allow is None else frozenset(map(canonical, allow))
    tables: list[tuple[str, str]] = []
    dists: list[str] = []
    try:
        eps = entry_points(group=LANDING_GROUP)
    except Exception as exc:  # noqa: BLE001 — discovery never takes the process down
        log.warning("landing_tables lookup failed: %s", exc)
        return [], []

    for ep in eps:
        dist = getattr(ep, "dist", None)
        name = canonical(getattr(dist, "name", "") or "")
        if not name or name not in allowed:
            log.warning(
                "landing_tables entry point %r from %r is not a portfolio member; skipped",
                ep.name,
                name or "<unknown>",
            )
            continue
        try:
            declared = ep.load()()
        except Exception as exc:  # noqa: BLE001 — one bad package must not hide the rest
            log.warning("landing_tables from %r failed to load: %s", name, exc)
            continue
        for pair in declared or ():
            schema, table = pair
            if (schema, table) not in tables:
                tables.append((str(schema), str(table)))
        dists.append(name)
    return tables, dists


def _portfolio() -> frozenset[str]:
    names: set[str] = set()
    try:
        eps = entry_points(group=PORTFOLIO_GROUP)
    except Exception as exc:  # noqa: BLE001
        log.warning("portfolio_member lookup failed: %s", exc)
        return frozenset()
    for ep in eps:
        dist = getattr(ep, "dist", None)
        if dist is not None and getattr(dist, "name", None):
            names.add(canonical(dist.name))
    return frozenset(names)


__all__ = ["LANDING_GROUP", "discover_landing_tables"]
