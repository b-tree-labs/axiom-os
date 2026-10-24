# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One introspection vocabulary, asked of any tier.

Four questions were split across six verbs and two naming schemes, with the
tier baked into the name — `gold_tables` and `bronze_inventory` asking the
same thing. A caller had to know which medallion they were on before they
could phrase the question, and every consumer downstream needed a branch per
tier to read the answer.
"""

from __future__ import annotations

import pytest

from ..medallion import (
    INTROSPECTION_VERBS,
    TIERS,
    NotAvailableOnTier,
    UnknownTier,
    envelope,
    resolve_tier,
    resolver_for,
)
from ..resolvers import BronzeResolver, TabularResolver, register_all

register_all()


# --- the vocabulary ---------------------------------------------------------


def test_every_tier_answers_every_introspection_verb():
    """Three of four and it looks complete while a question has no home."""
    for tier in TIERS:
        r = resolver_for(tier)
        for verb in INTROSPECTION_VERBS:
            assert callable(getattr(r, verb, None)), f"{tier} cannot answer {verb}"


def test_the_tier_is_an_argument_not_part_of_the_name():
    assert set(INTROSPECTION_VERBS) == {"catalog", "describe", "freshness", "sample"}
    assert not any(t in v for v in INTROSPECTION_VERBS for t in TIERS)


def test_bronze_is_answered_by_the_tree_and_the_others_by_the_catalog():
    assert isinstance(resolver_for("bronze"), BronzeResolver)
    assert isinstance(resolver_for("silver"), TabularResolver)
    assert isinstance(resolver_for("gold"), TabularResolver)


def test_gold_is_the_default_tier():
    assert resolve_tier(None) == "gold"


def test_an_unknown_tier_is_refused_rather_than_guessed():
    with pytest.raises(UnknownTier, match="not a medallion tier"):
        resolve_tier("platinum")


def test_public_is_not_a_medallion_tier():
    with pytest.raises(UnknownTier):
        resolve_tier("public")


# --- a tier that cannot answer says why -------------------------------------


def test_bronze_refuses_describe_and_says_what_to_ask_instead():
    """An empty column list is indistinguishable from a table that has none."""
    with pytest.raises(NotAvailableOnTier) as exc:
        BronzeResolver().describe({}, None)

    assert "no schema to describe" in str(exc.value)
    assert "catalog" in str(exc.value) and "sample" in str(exc.value)


def test_the_served_tiers_refuse_sample_and_say_why():
    with pytest.raises(NotAvailableOnTier) as exc:
        TabularResolver("gold").sample({}, None)

    assert "population guard" in str(exc.value)


def test_a_refusal_is_typed_so_it_can_be_told_from_an_unknown_tier():
    assert not issubclass(NotAvailableOnTier, UnknownTier)
    assert not issubclass(UnknownTier, NotAvailableOnTier)


# --- one envelope -----------------------------------------------------------


def test_every_tier_answers_in_the_same_shape():
    """A consumer branching on tier to read a result was paying for the split."""
    out = envelope(data={"objects": []}, tier="bronze", source="tree", method="walk", rows=0)

    assert set(out) == {"data", "provenance"}
    assert out["provenance"]["tier"] == "bronze"
    assert out["provenance"]["rows"] == 0


def test_the_envelope_names_which_tier_answered():
    """Otherwise a silver answer and a gold one are indistinguishable."""
    assert envelope(data=None, tier="silver", source="s", method="m")["provenance"]["tier"] == (
        "silver"
    )


# --- freshness means two different things, and both matter ------------------


def test_bronze_and_silver_freshness_are_different_questions():
    """A live producer whose conform pass stalled looks fresh on one, stale
    on the other, and telling those apart decides whether you chase a DAQ or
    a pipeline."""
    import inspect

    bronze = inspect.getdoc(BronzeResolver.freshness) or ""
    silver = inspect.getdoc(TabularResolver.freshness) or ""

    assert "conform" in (bronze + silver).lower()


def test_silver_freshness_calls_the_released_verb_rather_than_copying_it():
    """It grades against declared cadences and raises a HERALD alert; a second
    implementation would drift from the one that alerts."""
    import inspect

    assert "ingest_freshness" in inspect.getsource(TabularResolver.freshness)


# --- registration ------------------------------------------------------------


def test_the_skills_are_registered_under_the_shared_names():
    from .. import skills as data_skills

    registry = data_skills.bind_default()
    for verb in INTROSPECTION_VERBS:
        assert registry.has(f"data.{verb}"), verb


def test_the_released_verb_still_resolves():
    """Renaming an unreleased verb is free; breaking a released one is not."""
    from ..cli import _SKILL_ALIASES

    assert _SKILL_ALIASES[("ingest-freshness", None)] == "freshness"


def test_sample_is_not_projected_onto_mcp():
    """Catalog and freshness are counts and timestamps; sample hands back
    records from the substrate of record."""
    from .. import skills as data_skills

    registry = data_skills.bind_default()
    spec = registry.spec("data.sample")

    assert "mcp" not in (spec.surfaces or ())
