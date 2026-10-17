# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A readable name for a channel, laid OVER the one it was acquired under.

`NCDT1:HEAT:TC-CP1_1` is what somebody else's instrument calls a thing. It is
the identity of the channel and it is never rewritten: a rename forks the
data, and this programme has already paid for that once. So a friendly name is
a separate layer, applied when a figure is drawn and nowhere else.

## The default removes only what is REDUNDANT

An earlier version kept the last segment and dropped the rest, on the theory
that `NCDT1:HEAT` repeated fifteen times is noise. That was shortening for its
own sake, and it threw away a word — `HEAT` — that tells a reader which part
of the loop they are looking at. A shorter name that is harder to read is not
an improvement.

So the only thing dropped is a unit the DECLARATION already carries:
`corrected_cm` beside an axis labelled `cm` says it twice, and removing one of
them loses nothing, because the axis is still there. Everything else is kept.

A genuinely better short form needs judgement about what the parts MEAN —
which of `NCDT1`, `HEAT` and `TC-CP1_1` a reader needs, and what to call the
result. That is a proposal for a person to accept, not a rule to apply
silently, and it lives in :mod:`~axiom.extensions.builtins.webapp.views.suggest`.

## A label is the SITE's, not a person's

Somebody renaming a channel is naming the thing, not their view of it, and a
figure handed to a colleague has to read the same for both of them. Who set it
is kept, so a name that turns out to be wrong has somebody to ask.

The acquired name is never hidden: a surface shows the label and keeps the
original for a reader who asks, because the original is what the data is
keyed on and what anybody querying it will use.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

__all__ = ["SEGMENT", "default_label", "default_labels"]

#: How a hierarchical instrument name is written. Not a guess about a domain.
SEGMENT = re.compile(r"[:/]")

#: A trailing unit token the declaration already carries: `corrected_cm`.
_TRAILING_UNIT = re.compile(r"[_\-]([A-Za-z%°]{1,6})$")


def _drop_redundant_unit(name: str, unit: str) -> str:
    """The name, minus a trailing unit the declaration already states.

    Only when they MATCH. `measured_cm` with no declared unit keeps its
    suffix, because that suffix is the only place the unit appears at all and
    dropping it would hide the one clue the channel gives.
    """
    if not unit:
        return name
    match = _TRAILING_UNIT.search(name)
    if match and match.group(1).lower() == unit.lower().lstrip("°"):
        trimmed = name[: match.start()]
        if trimmed:
            return trimmed
    return name


def default_labels(channels: Iterable[tuple[str, str]]) -> dict[str, str]:
    """``{channel: label}`` for a set drawn together.

    *channels* is ``(name, unit)``. Decided over the whole set rather than per
    name, because any shortening is only safe while the results still differ:
    two channels that would come out identical keep their full names, both of
    them, so a reader is never choosing between two matching chips.
    """
    pairs = list(channels)
    proposed = {name: _drop_redundant_unit(name, unit or "") for name, unit in pairs}
    taken: dict[str, int] = {}
    for label in proposed.values():
        taken[label] = taken.get(label, 0) + 1
    return {
        name: (label if taken[label] == 1 else name)
        for name, label in proposed.items()
    }


def default_label(name: str, unit: str = "") -> str:
    """The default for one channel, with nothing to collide against."""
    return default_labels([(name, unit)])[name]
