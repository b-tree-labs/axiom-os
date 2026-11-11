# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Carrying correlation across a boundary.

The gap this closes
-------------------
The ``uncertainties`` package is the established reference for linear
propagation with automatic correlation tracking, and it cannot be used here
for one reason: its correlation lives in **in-process Python object
identity**. Two of its numbers are correlated because they hold references
to the same variable object. Write them to Postgres, serialise them over
MCP, or send them to a peer node and that identity is gone — the numbers
arrive independent, and independent is the *narrow* direction.

There is no library whose correlation is a **name**. That is what this
package's affine form gives, and this module is what makes the name survive
the trip.

The collision that would make a federated answer WRONG
------------------------------------------------------
Symbols are namespaced per extension (ADR-136 D3):
``<extension>:<resource>:<aspect>``. That is enough inside one node and not
enough between two.

Two sites both running this platform will both mint
``signals:tc-14:repeatability`` — for **different physical sensors**. Compose
their readings with bare symbols and the algebra concludes the two sites
share a source. The shared part then cancels in a difference and fails to
average away in a mean, and the served answer is **too confident** about a
correlation that does not exist.

That is the dangerous direction. A bound that is too wide is a nuisance; a
bound that is too narrow is a wrong answer wearing a provenance trail.

So a symbol crossing a boundary is **qualified by its origin**:

    @site-alpha/signals:tc-14:repeatability
    @site-beta/signals:tc-14:repeatability

Those never match, so cross-origin composition yields zero correlation —
wider, and safe. Same-origin composition matches exactly, so correlation
survives the round trip intact.

The invariant
-------------
**Anyone may widen; nobody may narrow by assertion.**

Qualification enforces it structurally rather than by policy. A foreign
contribution can add sources, and adding sources can only widen or leave
unchanged. Narrowing requires claiming that two origins' symbols are the
same physical source, which :func:`alias` makes an explicit, auditable act
rather than something that happens by default when two names coincide.

Bare symbols stay bare in storage. The companion table holds local symbols,
qualification happens at the edge, and nothing in the database has to know
which node will eventually read it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from axiom.uncertainty import _BARE_SYMBOL, _ORIGIN_PREFIX, Quantity, check_symbol

#: Wire format version. Present so a reader can refuse what it does not
#: understand rather than silently mis-parsing a future shape — a quantity
#: parsed wrong is a wrong number, not a wrong message.
WIRE_VERSION = 1

#: Origins are principals in the platform's own form: ``@name`` or
#: ``@name:context``, single leading ``@``.
_ORIGIN = re.compile(r"^@[A-Za-z0-9_\-.]+(?::[A-Za-z0-9_\-.]+)?$")

#: ``<origin>/<extension>:<resource>:<aspect>``. Built from the SAME pieces
#: the package's own grammar uses, so the two cannot drift: a symbol this
#: module qualifies must be one `check_symbol` accepts, and a second regex
#: kept in step by hand would eventually not be.
_QUALIFIED = re.compile(rf"^{_ORIGIN_PREFIX}{_BARE_SYMBOL}$")

#: What separates the origin from the symbol. Chosen because it cannot
#: appear in either — origins use ``:`` for context and symbols use ``:``
#: between parts, so a second delimiter is needed and it must be one the
#: symbol grammar excludes.
SEPARATOR = "/"


class WireError(ValueError):
    """A payload that cannot be trusted to mean what it says."""


def check_origin(origin: str) -> str:
    if not isinstance(origin, str) or not _ORIGIN.match(origin):
        raise WireError(
            f"origin {origin!r} is not a principal — expected '@name' or "
            "'@name:context' with a single leading '@' (ADR-020). The origin "
            "is what stops two nodes' identically-named sources being treated "
            "as one, so it cannot be guessed or omitted."
        )
    return origin


def is_qualified(symbol: str) -> bool:
    return bool(_QUALIFIED.match(symbol))


def qualify(symbol: str, *, origin: str) -> str:
    """``signals:tc-14:rep`` + ``@site-alpha`` -> ``@site-alpha/signals:tc-14:rep``.

    Idempotent only for the SAME origin: re-qualifying a symbol that already
    carries a different origin is refused rather than nested, because
    ``@a/@b/sym`` would be a source that belongs to nobody and would match
    nothing.
    """
    check_origin(origin)
    if is_qualified(symbol):
        existing = origin_of(symbol)
        if existing != origin:
            raise WireError(
                f"symbol {symbol!r} already carries origin {existing!r} and cannot "
                f"be re-qualified as {origin!r}. Re-origining a foreign source "
                "would claim somebody else's measurement as your own, and would "
                "make it correlate with your sources when it does not."
            )
        return symbol
    check_symbol(symbol)
    return f"{origin}{SEPARATOR}{symbol}"


def origin_of(qualified: str) -> str | None:
    """The origin, or ``None`` for a bare local symbol."""
    if not is_qualified(qualified):
        return None
    return qualified.split(SEPARATOR, 1)[0]


def local_part(qualified: str) -> str:
    """The symbol without its origin.

    Deliberately NOT used to compare symbols across origins. Two locals can
    coincide while describing different instruments, and matching on the
    local part is exactly the collision this module exists to prevent.
    """
    return qualified.split(SEPARATOR, 1)[1] if is_qualified(qualified) else qualified


def qualify_terms(terms: Mapping[str, float], *, origin: str) -> dict[str, float]:
    return {qualify(sym, origin=origin): a for sym, a in terms.items()}


def to_wire(quantity: Quantity, *, origin: str) -> dict:
    """The JSON-safe form, with every symbol qualified.

    ``origin`` is required and has no default. A default would be the one
    thing that could reintroduce the collision: a caller who forgot would
    emit bare symbols that a peer's algebra would happily match against its
    own.
    """
    check_origin(origin)
    return {
        "v": WIRE_VERSION,
        "value": float(quantity.value),
        "unit": quantity.unit,
        "origin": origin,
        "terms": qualify_terms(quantity.terms, origin=origin),
    }


def from_wire(payload: Mapping) -> Quantity:
    """Reconstruct a quantity, refusing anything that could compose wrongly.

    Every symbol must arrive qualified. An unqualified symbol on the wire is
    refused rather than qualified on arrival with the sender's claimed
    origin: the receiver cannot know whether the sender meant a local source
    or forgot to qualify, and guessing wrong in the "local" direction creates
    a false correlation that narrows the answer.
    """
    if not isinstance(payload, Mapping):
        raise WireError(f"expected a mapping, got {type(payload).__name__}")
    version = payload.get("v")
    if version != WIRE_VERSION:
        raise WireError(
            f"wire version {version!r} is not {WIRE_VERSION}; refusing to parse "
            "rather than risk reading a future shape as though it were this one"
        )
    terms = payload.get("terms") or {}
    if not isinstance(terms, Mapping):
        raise WireError("'terms' must be a mapping of symbol to coefficient")
    unqualified = [s for s in terms if not is_qualified(s)]
    if unqualified:
        raise WireError(
            f"refusing {len(unqualified)} unqualified symbol(s) including "
            f"{unqualified[0]!r}. A bare symbol from outside this node would "
            "match a local source of the same name and make two different "
            "instruments look like one, which narrows the answer — the "
            "direction that is wrong rather than merely unhelpful."
        )
    value = payload.get("value")
    unit = payload.get("unit")
    if not isinstance(value, (int, float)):
        raise WireError(f"'value' must be a number, got {type(value).__name__}")
    if not isinstance(unit, str):
        raise WireError("'unit' must be a string; a value without its unit is not a fact")
    return Quantity(
        value=float(value),
        unit=unit,
        terms={str(s): float(a) for s, a in terms.items()},
    )


def alias(quantity: Quantity, mapping: Mapping[str, str]) -> Quantity:
    """Declare that distinct qualified symbols ARE the same physical source.

    The only operation here that can NARROW an answer, so it is explicit,
    auditable, and never a default.

    It is occasionally correct. Two sites calibrated against the same
    national standard genuinely share that source, and their readings genuinely
    do correlate through it — which means a difference between them is more
    precise than either, and refusing to say so overstates. But the claim has
    to be made by somebody who knows, not inferred from two names matching.

    ``mapping`` maps a symbol present on the quantity to the canonical symbol
    it should be merged into. Coefficients of merged symbols ADD, which is
    what sharing a source means.
    """
    if not mapping:
        return quantity
    merged: dict[str, float] = {}
    for sym, a in quantity.terms.items():
        target = mapping.get(sym, sym)
        if target != sym and not is_qualified(target) and is_qualified(sym):
            raise WireError(
                f"cannot alias qualified {sym!r} onto bare {target!r} — the "
                "canonical symbol for a shared source must itself say whose "
                "source it is, or it will collide with a local one"
            )
        merged[target] = merged.get(target, 0.0) + a
    return Quantity(value=quantity.value, unit=quantity.unit, terms=merged)


__all__ = [
    "SEPARATOR",
    "WIRE_VERSION",
    "WireError",
    "alias",
    "check_origin",
    "from_wire",
    "is_qualified",
    "local_part",
    "origin_of",
    "qualify",
    "qualify_terms",
    "to_wire",
]
