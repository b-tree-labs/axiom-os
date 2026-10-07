# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""There are two quality vocabularies and they do not agree.

Measured on a real install, 2026-09-30:

    quality       rows   in ADR-132?   accepted at ingest?
    good        618,559      yes            yes
    uncertain    28,000      NO             yes
    bad               9      yes            yes
    ok                1      NO             NO

28,001 of 646,569 rows — 4.3% — carry a verdict ADR-132 does not define, so a
consumer filtering on the taxonomy silently drops every one of them. And it
cuts the other way too: `suspect`, `saturated` and `stale` are what the fault
detector in `company.py` PRODUCES, and the ingest envelope refuses all three.

So a partner writing an emitter against the published contract can say
`uncertain`, which nothing downstream understands, and cannot say `suspect`,
which is what the platform's own detector would have said about the same
reading. Neither half is wrong on its own; they were written at different
times for different jobs and nothing has ever compared them.

This test does not pick a winner — which vocabulary wins, and how `uncertain`
maps onto it, is a decision with partner-visible consequences. It pins the
disagreement so it cannot drift further unnoticed, and so that reconciling it
has somewhere obvious to start.
"""

from __future__ import annotations


def test_the_two_vocabularies_are_still_what_they_were():
    """Pinned deliberately. If either moves, this fails and whoever moved it
    has to look at the other one — which is the whole problem: nobody ever
    had to."""
    from axiom.extensions.builtins.data_platform.company import QUALITY
    from axiom.extensions.builtins.data_platform.daq.envelope import QUALITIES

    assert QUALITY == ("good", "suspect", "bad", "saturated", "stale")
    assert QUALITIES == ("good", "uncertain", "bad")


def test_the_disagreement_is_named_in_both_directions():
    """A reader of this failure should not have to work out which way it bites.

    It bites both ways, and the second direction is the one that surprises:
    the platform's own detector emits verdicts its own ingest refuses.
    """
    from axiom.extensions.builtins.data_platform.company import QUALITY
    from axiom.extensions.builtins.data_platform.daq.envelope import QUALITIES

    accepted_but_undefined = [q for q in QUALITIES if q not in QUALITY]
    produced_but_refused = [q for q in QUALITY if q not in QUALITIES]

    assert accepted_but_undefined == ["uncertain"], (
        "a value the ingest accepts that the taxonomy does not define lands in "
        "gold and is invisible to every consumer that filters on the taxonomy"
    )
    assert produced_but_refused == ["suspect", "saturated", "stale"], (
        "the fault detector produces these and the ingest envelope refuses "
        "them, so the platform cannot send itself its own verdict"
    )


def test_the_column_default_is_a_value_the_taxonomy_defines():
    """`ok` was the declared default and is in neither vocabulary. It reached
    exactly one row in seventy million, which is how long a fourth spelling
    stayed invisible. The DDL now sets it to `good`; this is what stops it
    coming back."""
    from axiom.extensions.builtins.data_platform.company import QUALITY
    from axiom.extensions.builtins.data_platform.conformance import SILVER_SIGNALS_DDL

    ddl = "\n".join(SILVER_SIGNALS_DDL)
    assert "DEFAULT 'ok'" not in ddl or "SET DEFAULT 'good'" in "\n".join(SILVER_SIGNALS_DDL)
    assert "good" in QUALITY
