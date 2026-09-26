# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Domain-normalizer discovery — platform packages only (ADR-023-A1 §A1.3).

Conformance mechanics are domain-agnostic and live here; the normalizers that
know a reactor's schemas live in downstream platform packages. Something has
to introduce them, and that introduction is a **security boundary**, not a
convenience: one conform process reads every tenant's bronze rows in one
interpreter, so a normalizer contributed by a site package would be one
institution's code executing against another institution's data.

So discovery honours the ``axiom.data_platform.normalizers`` entry-point group
**only when the distribution declaring it is a portfolio member** — that is,
when it also declares ``axiom.portfolio_member``. Portfolio membership is
controlled by the platform, not by a deployment, which makes "platform code
only" a property of the mechanism rather than a rule reviewers must remember.

A site with a genuinely bespoke instrument contributes a **provider** instead:
that runs at the edge, in the site's own process, over the site's own data,
where its blast radius is itself.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

log = logging.getLogger(__name__)

#: Entry-point group a platform package uses to register domain normalizers.
#: The value is a callable taking the registry: ``register_all(registry)``.
NORMALIZER_GROUP = "axiom.data_platform.normalizers"

#: Declaring this group is what marks a distribution as platform code.
PORTFOLIO_GROUP = "axiom.portfolio_member"


def portfolio_distributions() -> frozenset[str]:
    """Names of installed distributions that declare portfolio membership."""
    names: set[str] = set()
    try:
        eps = entry_points(group=PORTFOLIO_GROUP)
    except Exception as exc:  # noqa: BLE001 — discovery never takes the process down
        log.warning("portfolio_member lookup failed: %s", exc)
        return frozenset()
    for ep in eps:
        dist = getattr(ep, "dist", None)
        if dist is not None and getattr(dist, "name", None):
            names.add(_canonical(dist.name))
    return frozenset(names)


def _canonical(name: str) -> str:
    """PEP 503 normalization, so `Axiom-OS-LM` and `axiom_os_lm` agree."""
    return name.lower().replace("_", "-").replace(".", "-")


def register_discovered(registry, *, allow: frozenset[str] | None = None) -> list[str]:
    """Register every platform-supplied normalizer into ``registry``.

    Returns the distribution names whose normalizers were registered, so a
    caller can log or assert on what it actually loaded — a conform run that
    silently found nothing is the failure mode worth making visible.

    ``allow`` overrides the portfolio lookup (tests, and a deployment that
    deliberately pins the set). An entry point from a distribution outside the
    allowed set is **skipped and logged**, never loaded: refusing to import is
    the point, since importing is already execution.
    """
    allowed = portfolio_distributions() if allow is None else frozenset(map(_canonical, allow))
    loaded: list[str] = []
    try:
        eps = entry_points(group=NORMALIZER_GROUP)
    except Exception as exc:  # noqa: BLE001
        log.warning("normalizer entry-points lookup failed: %s", exc)
        return loaded

    for ep in eps:
        dist = getattr(ep, "dist", None)
        name = _canonical(getattr(dist, "name", "") or "")
        if not name or name not in allowed:
            log.warning(
                "refusing normalizer %r from %r — not a portfolio distribution; "
                "a site contributes a provider, not a normalizer (ADR-023-A1 A1.3)",
                ep.name,
                name or "<unknown>",
            )
            continue
        try:
            register = ep.load()
            register(registry)
        except Exception as exc:  # noqa: BLE001 — one bad package must not stop the rest
            log.warning("normalizer %r from %r failed to register: %s", ep.name, name, exc)
            continue
        loaded.append(name)
    return loaded


__all__ = [
    "NORMALIZER_GROUP",
    "PORTFOLIO_GROUP",
    "portfolio_distributions",
    "register_discovered",
]
