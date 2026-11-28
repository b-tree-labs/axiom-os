# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-265 D7: one way to say how sure a reading is, on every surface.

A reading's uncertainty reaches a renderer in three shapes: a bare magnitude
(an observation's ``Reading``), a served bound (the result envelope's
``uncertainty``, a dict), or nothing. Each surface used to word them itself,
so the same value read "± 0.5", "0.5" or nothing at all depending on where it
was looked at. One function words all three, and absence always reads
"not reported", never as zero and never as a blank.
"""

from __future__ import annotations

from axiom.observation import Reading, uncertainty_words
from axiom.uncertainty.serving import Served


def test_absence_reads_not_reported_never_zero_or_blank():
    assert uncertainty_words(None, "K") == "not reported"
    assert uncertainty_words(Served(fn="max", unquantified=1).payload(), "K") == "not reported"


def test_a_bare_magnitude_reads_plus_or_minus_in_the_values_unit():
    assert uncertainty_words(0.5, "K") == "± 0.5 K"
    assert uncertainty_words(0.5, "") == "± 0.5"
    assert uncertainty_words(0.0, "K") == "± 0 K"  # a reported zero is a claim, and says so


def test_a_served_bound_reads_the_same_way_whether_exact_or_a_range():
    exact = Served(fn="max", low=0.5, high=0.5, quantified=1).payload()
    assert uncertainty_words(exact, "K") == "± 0.5 K"
    ranged = Served(fn="mean", low=0.2, high=0.4, quantified=4, premise="independent").payload()
    assert uncertainty_words(ranged, "K") == "± 0.2 to 0.4 K"


def test_a_served_object_and_its_wire_payload_read_alike():
    s = Served(fn="max", low=0.5, high=0.5, quantified=1)
    assert uncertainty_words(s, "K") == uncertainty_words(s.payload(), "K")


def test_the_reading_family_uses_the_same_words():
    r = Reading(value=312.4, unit="K", quality="good", uncertainty=0.5)
    assert r.uncertainty_words() == uncertainty_words(0.5, "K")
    assert Reading(value=1.0, unit="K", quality="good").uncertainty_words() == "not reported"
