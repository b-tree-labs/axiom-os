# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One introspection vocabulary for every medallion tier.

Four questions are askable of any tier, and they had three different names
between them:

============  =========================  =======================
question      was                        is
============  =========================  =======================
what is here  ``gold_tables`` /          ``catalog``
              ``bronze_inventory``
shape of one  ``gold_describe``          ``describe``
how current   ``ingest_freshness`` /     ``freshness``
              ``bronze_freshness``
show me some  ``bronze_peek``            ``sample``
============  =========================  =======================

The tier was baked into the verb NAME, so a caller had to know which
medallion they were on before they could phrase the question — which defeats
the point of having a uniform medallion. It also meant two implementations
of "what is here" that shared no code and, worse, no result shape, so every
consumer downstream needed a branch per tier.

Now the tier is an argument and the vocabulary is fixed. A tier supplies a
**resolver** answering the same four questions in the same envelope; what
differs is only where it looks. Bronze walks a filesystem tree because
bronze IS a filesystem tree; silver and gold introspect the catalog because
they are tables. That difference belongs in a resolver, not in a verb name.

Answering verbs — ``aggregate``, ``series`` — are a separate family. They
compute rather than introspect, they are tabular-only, and they take the
same ``tier`` argument without joining this set.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

#: Every medallion tier, in the order data moves through them.
TIERS: tuple[str, ...] = ("bronze", "silver", "gold")

#: Tiers the tabular resolver serves. Bronze is absent because it is not
#: tables; that is a fact about bronze, not a gap.
TABULAR_TIERS: tuple[str, ...] = ("silver", "gold")

#: What a tier is asked. Named here so a new tier cannot answer three of
#: four and look complete.
INTROSPECTION_VERBS: tuple[str, ...] = ("catalog", "describe", "freshness", "sample")


class UnknownTier(ValueError):
    """A tier outside :data:`TIERS`."""


class NotAvailableOnTier(ValueError):
    """The verb is real and this tier cannot answer it.

    Distinct from :class:`UnknownTier`: "bronze has no aggregate" is a fact
    about bronze, and saying so is more useful than a generic refusal.
    """


def resolve_tier(tier: str | None, *, default: str = "gold") -> str:
    """Validate a caller-named tier. An allowlist, never caller-supplied text."""
    name = (str(tier).strip().lower() if tier else default)
    if name not in TIERS:
        raise UnknownTier(f"{tier!r} is not a medallion tier; expected one of {', '.join(TIERS)}")
    return name


class Resolver(Protocol):
    """What a tier must be able to answer. All four, or it is not a tier."""

    def catalog(self, params: dict[str, Any], ctx: Any) -> dict[str, Any]: ...

    def describe(self, params: dict[str, Any], ctx: Any) -> dict[str, Any]: ...

    def freshness(self, params: dict[str, Any], ctx: Any) -> dict[str, Any]: ...

    def sample(self, params: dict[str, Any], ctx: Any) -> dict[str, Any]: ...


#: tier -> zero-argument factory. Lazy so importing this module does not
#: import a database driver to answer a question about a directory.
_RESOLVERS: dict[str, Callable[[], Resolver]] = {}


def register_resolver(tier: str, factory: Callable[[], Resolver]) -> None:
    """Bind a tier to the thing that answers for it."""
    _RESOLVERS[resolve_tier(tier)] = factory


def resolver_for(tier: str | None, *, default: str = "gold") -> Resolver:
    name = resolve_tier(tier, default=default)
    factory = _RESOLVERS.get(name)
    if factory is None:
        raise NotAvailableOnTier(
            f"no resolver is registered for the {name} tier — the tier is real "
            "and nothing in this install can answer for it"
        )
    return factory()


def envelope(
    *,
    data: Any,
    tier: str,
    source: str,
    method: str,
    rows: int | None = None,
    note: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """The one shape every tier answers in.

    A consumer that had to branch on tier to read a result was paying for
    the inconsistency this module removes.
    """
    prov: dict[str, Any] = {"tier": tier, "source": source, "method": method}
    if rows is not None:
        prov["rows"] = rows
    if note:
        prov["note"] = note
    prov.update({k: v for k, v in extra.items() if v is not None})
    return {"data": data, "provenance": prov}


__all__ = [
    "INTROSPECTION_VERBS",
    "TABULAR_TIERS",
    "TIERS",
    "NotAvailableOnTier",
    "Resolver",
    "UnknownTier",
    "envelope",
    "register_resolver",
    "resolve_tier",
    "resolver_for",
]
