# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Where a ``readings`` field's instruments come from (spec-attestation, Field sources).

A logbook declares ``from_site = "<source>"`` on a readings field. The extension
that owns the site's configuration registers that source: a function from a
site id to the instruments read there. attest never reads a site's
configuration itself; it asks the source.

An instrument's ``uncertainty`` is what the site declares for it, in the
three kinds of :mod:`axiom.uncertainty`: ``{"kind": "sources", ...}``,
``{"kind": "magnitude_only", "u": "<decimal>"}`` or nothing, which a reading
records as ``{"kind": "unquantified"}``. Never zero for unknown.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Instrument:
    id: str
    label: str
    unit: str
    uncertainty: dict[str, Any] | None = field(default=None)


Source = Callable[[str], list[Instrument]]

_sources: dict[str, Source] = {}


def register(name: str, source: Source) -> None:
    _sources[name] = source


def unregister(name: str) -> None:
    _sources.pop(name, None)


def resolve(name: str, site_id: str) -> list[Instrument]:
    """The instruments ``name`` lists for ``site_id``; raises ``LookupError``
    when no extension registered that source."""
    try:
        source = _sources[name]
    except KeyError:
        raise LookupError(
            f"no extension registered the field source {name!r}; the logbook names it "
            "but nothing on this node can say which instruments it means"
        ) from None
    return list(source(site_id))


__all__ = ["Instrument", "Source", "register", "resolve", "unregister"]
