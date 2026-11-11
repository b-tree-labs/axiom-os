# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Which producers are standing in for hardware that does not exist yet.

``source_class`` says what KIND of thing a reading is — measured, predicted,
estimated, simulated. It does not say whether anybody meant it.

Those are different questions, and conflating them is how a platform ends up
serving its own test data as a result. A physics simulation and a producer
written to exercise an ingest path are both ``simulated``: both are numbers
a model produced rather than an instrument. But one is an artefact somebody
will cite and the other is scaffolding, and only the second should disappear
when the scaffolding comes down.

So the distinction lives where the difference actually is — in the PRODUCER,
named by ``model_ref``. A deployment declares which of its producers are
fixtures; nothing about a row changes, and no schema moves.

That matters right now because staging is production. Declaring is
reversible and needs no migration on a node that has not had one in months;
a column would need both, and would need deciding before anybody has seen
what the answer costs. When a fixture tier does exist, this is the list that
says what moves into it.

Patterns are shell globs against ``model_ref``, so a family of producers is
one line::

    flowloop-sim/*

Nothing is a fixture by default. A platform that guessed would eventually
guess that somebody's real model was scaffolding and drop it from a figure
without saying so, which is the worst failure available here.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable, Sequence

#: Where a deployment declares its fixture producers: globs, comma-separated.
FIXTURE_ENV = "AXIOM_FIXTURE_MODELS"

#: What a caller may ask to see. `exclude` is the production view: the data
#: is still there, and a surface that wants it says so out loud.
SHOW_EXCLUDE = "exclude"
SHOW_INCLUDE = "include"
SHOW_ONLY = "only"
SHOWS = (SHOW_EXCLUDE, SHOW_INCLUDE, SHOW_ONLY)


def declared_fixtures(
    env: dict[str, str] | None = None, *, extra: Iterable[str] = ()
) -> tuple[str, ...]:
    """The fixture patterns this deployment declares.

    ``extra`` is for a consumer that ships its own producers and therefore
    knows they are fixtures without being told — a site simulator in its own
    repository, say. The environment ADDS to it rather than replacing it, so
    an operator cannot un-declare somebody's scaffolding by accident.
    """
    source = os.environ if env is None else env
    said = [p.strip() for p in (source.get(FIXTURE_ENV, "") or "").split(",")]
    return tuple(dict.fromkeys([*(e for e in extra if e), *(s for s in said if s)]))


def is_fixture(model_ref: str | None, patterns: Sequence[str]) -> bool:
    """Is this producer scaffolding?

    A row with no ``model_ref`` is never a fixture: nothing produced it but
    an instrument, and a measurement cannot be scaffolding.
    """
    if not model_ref:
        return False
    return any(fnmatch.fnmatch(model_ref, p) for p in patterns)


def sql_predicate(show: str, patterns: Sequence[str], column: str = "model_ref") -> tuple[str, list]:
    """``(clause, params)`` restricting a query to what *show* asks for.

    Returned as a clause rather than applied here, because the caller owns
    its query and a helper that builds whole statements ends up owning
    everybody's. An empty clause means "no restriction" — which is what a
    deployment that has declared nothing should get, in every mode.

    Patterns are matched in SQL with LIKE over a translated glob, so the
    database does the filtering and a figure never fetches rows it will
    throw away. Only `*` and `?` translate; anything else is taken
    literally, because a declaration is a producer name and not a regex.
    """
    if show not in SHOWS:
        raise ValueError(f"show must be one of {', '.join(SHOWS)}; got {show!r}")
    if not patterns or show == SHOW_INCLUDE:
        return "", []
    likes = [_glob_to_like(p) for p in patterns]
    matched = " OR ".join([f"{column} LIKE %s"] * len(likes))
    if show == SHOW_ONLY:
        return f"({matched})", likes
    # Excluding: a NULL model_ref is not a fixture, and `NOT (NULL LIKE …)`
    # is NULL, which a WHERE clause treats as false — so an unqualified NOT
    # would silently drop every measured reading in the table.
    return f"({column} IS NULL OR NOT ({matched}))", likes


def _glob_to_like(pattern: str) -> str:
    out = pattern.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    return out.replace("*", "%").replace("?", "_")
