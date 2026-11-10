# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Finding the checks a platform package supplies.

Mirrors :mod:`..conformance.discovery` deliberately, including the part that
looks restrictive: an entry point is only loaded when it comes from a
distribution that declares portfolio membership.

That is an authority boundary, not bureaucracy. Loading an entry point means
importing and calling code, so the question "may this package register a QC
check?" is the same question as "may this package run in the promotion path?".
A deployment contributes configuration; a platform package contributes checks.

Practically, for a contributor: your distribution declares two entry points, one
marking it as portfolio code and one pointing at your registration function.

.. code-block:: toml

    # pyproject.toml
    [project.entry-points."axiom.portfolio_member"]
    my-package = "my_package:__name__"

    [project.entry-points."axiom.data_platform.validators"]
    my-package = "my_package.checks:register_all"

``register_all`` takes the registry and registers whatever it owns:

.. code-block:: python

    def register_all(registry):
        registry.register("model:example-surrogate@3", "tracks-reference", reading_bounds)
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

from axiom.extensions.builtins.data_platform.conformance.discovery import (
    PORTFOLIO_GROUP,
    portfolio_distributions,
)

log = logging.getLogger(__name__)

#: Entry-point group a platform package uses to register domain QC checks.
#: The value is a callable taking the registry: ``register_all(registry)``.
VALIDATOR_GROUP = "axiom.data_platform.validators"

__all__ = [
    "PORTFOLIO_GROUP",
    "VALIDATOR_GROUP",
    "register_discovered",
]


def _canonical(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def register_discovered(registry, *, allow: frozenset[str] | None = None) -> list[str]:
    """Register every platform-supplied check into ``registry``.

    Returns the distribution names whose checks were registered, so a caller can
    log or assert on what it actually loaded. A promotion run that silently
    found no checks and therefore promoted everything is the failure mode worth
    making visible, and it looks identical to a healthy run unless somebody
    counts.

    ``allow`` overrides the portfolio lookup, for tests and for a deployment
    that deliberately pins the set. An entry point from a distribution outside
    the allowed set is **skipped and logged**, never loaded, because importing
    is already execution.
    """
    allowed = portfolio_distributions() if allow is None else frozenset(map(_canonical, allow))
    loaded: list[str] = []
    try:
        eps = entry_points(group=VALIDATOR_GROUP)
    except Exception as exc:  # noqa: BLE001 — discovery never takes the process down
        log.warning("validator entry-points lookup failed: %s", exc)
        return loaded

    for ep in eps:
        dist = getattr(ep, "dist", None)
        name = _canonical(getattr(dist, "name", "") or "")
        if not name or name not in allowed:
            log.warning(
                "refusing validator %r from %r — not a portfolio distribution; "
                "a site contributes configuration, not a QC check",
                ep.name,
                name or "<unknown>",
            )
            continue
        try:
            register = ep.load()
            register(registry)
        except Exception as exc:  # noqa: BLE001 — one bad package must not stop the rest
            log.warning("validator %r from %r failed to register: %s", ep.name, name, exc)
            continue
        loaded.append(name)
    return loaded
