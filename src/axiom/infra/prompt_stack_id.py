# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A stable name for the prompt stack a turn was composed from.

Observability already records *which* contributions made up a system prompt, so
any single answer is attributable. What was missing is the other direction:
grouping. Two thousand turns that shared one prompt design cannot be compared
against two thousand that shared another unless both designs have a name.

That name is a hash of the composed stack, and it is what an experiment keys on:
"this arm is stack a1b2c3, that arm is d4e5f6", with the quality scores already
being written alongside.

What goes into it is the whole design decision.

Only the cacheable layers count. The per-turn layers — retrieved context, live
state — differ on every turn by construction, so including them would give every
turn a unique id and make grouping impossible, which is the opposite of the
point. The boundary is not invented here: it is the same cache boundary the
composer already draws, reused rather than redefined.

Content counts, not just names. A fragment edited in place is a different design
and must hash differently, or an experiment would silently compare a prompt
against its own later revision and report the difference as noise.

Order counts. The same fragments in a different order compose a different
prompt, so they are a different stack.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from typing import Any

from axiom.infra.prompt_composer import CACHEABLE_LAYERS, LAYERS

#: Short enough to read in a trace and paste into a query; long enough that a
#: collision between two live prompt designs is not a practical concern.
_ID_LENGTH = 16


def fragment_id(contribution: Any) -> str:
    """A readable, content-sensitive id for one contribution.

    ``layer/name@<content digest>`` — the name says what it is, the digest says
    which version of it, so an edited fragment is visibly a different fragment
    rather than the same one behaving differently.
    """
    content = getattr(contribution, "content", "") or ""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:8]
    layer = getattr(contribution, "layer", "?")
    name = getattr(contribution, "name", "?")
    return f"{layer}/{name}@{digest}"


def _stack_members(
    contributions: Iterable[Any], include_layers: Sequence[str]
) -> list[Any]:
    """Contributions in the layers that define the design, in composed order.

    Sorted by layer position rather than by name: the composer emits layers in
    a fixed order and that order is part of the prompt, so the id has to respect
    it. Within a layer the given order is kept, because that is the order the
    text will appear in.
    """
    position = {layer: index for index, layer in enumerate(LAYERS)}
    allowed = set(include_layers)
    members = [c for c in contributions if getattr(c, "layer", None) in allowed]
    return sorted(members, key=lambda c: position.get(getattr(c, "layer", ""), 99))


def stack_fragments(
    contributions: Iterable[Any],
    *,
    include_layers: Sequence[str] = CACHEABLE_LAYERS,
) -> list[str]:
    """The fragment ids that define this stack, in composition order."""
    return [fragment_id(c) for c in _stack_members(contributions, include_layers)]


def stack_id(
    contributions: Iterable[Any],
    *,
    include_layers: Sequence[str] = CACHEABLE_LAYERS,
) -> str:
    """A stable id for the prompt design these contributions compose.

    Deterministic across processes and machines: it hashes the fragment ids, and
    a fragment id is a name plus a digest of its own text. Nothing about the
    turn, the principal or the clock enters it.
    """
    fragments = stack_fragments(contributions, include_layers=include_layers)
    if not fragments:
        # An empty stack is a real state — a composer with nothing registered —
        # and it needs an id that is stable and obviously not a real design.
        return "empty"
    joined = "\n".join(fragments).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()[:_ID_LENGTH]


def describe_stack(
    contributions: Iterable[Any],
    *,
    include_layers: Sequence[str] = CACHEABLE_LAYERS,
) -> dict[str, Any]:
    """The id together with what it is an id *of*.

    An experiment that records only the hash can group turns but cannot explain
    a result. Carrying the fragment list means the winning arm can be read.
    """
    fragments = stack_fragments(contributions, include_layers=include_layers)
    return {
        "stack_id": stack_id(contributions, include_layers=include_layers),
        "fragments": fragments,
        "layers_hashed": list(include_layers),
        "fragment_count": len(fragments),
    }


__all__ = [
    "describe_stack",
    "fragment_id",
    "stack_fragments",
    "stack_id",
]
