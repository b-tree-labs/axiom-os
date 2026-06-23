# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axiom.governance.classification`.

The `Classification` enum is the data-tier label every `ActionEnvelope`
carries (spec-governance-fabric §1.2). The tier ordering is load-bearing:
every primitive that does classification routing compares envelope
classification against channel/resource ceilings.
"""

from __future__ import annotations

import pytest

from axiom.governance.classification import Classification, classification_lte


class TestClassificationOrdering:
    """The tier ordering is the load-bearing semantic."""

    def test_public_is_lowest(self):
        assert Classification.PUBLIC.tier == 0

    def test_controlled_is_highest(self):
        assert Classification.CONTROLLED.tier == 3

    def test_order_strictly_increasing(self):
        tiers = [
            Classification.PUBLIC,
            Classification.INTERNAL,
            Classification.REGULATED,
            Classification.CONTROLLED,
        ]
        for a, b in zip(tiers, tiers[1:]):
            assert a.tier < b.tier, f"{a} should be strictly lower than {b}"

    def test_classification_lte_within_ceiling(self):
        assert classification_lte(Classification.PUBLIC, Classification.INTERNAL)
        assert classification_lte(Classification.INTERNAL, Classification.INTERNAL)
        assert classification_lte(Classification.REGULATED, Classification.CONTROLLED)

    def test_classification_lte_above_ceiling_rejected(self):
        assert not classification_lte(Classification.CONTROLLED, Classification.PUBLIC)
        assert not classification_lte(Classification.REGULATED, Classification.INTERNAL)


class TestClassificationSerialization:
    """Classifications must serialize as plain strings (JSON / TOML stable)."""

    @pytest.mark.parametrize("c", list(Classification))
    def test_value_is_lowercase_string(self, c):
        assert isinstance(c.value, str)
        assert c.value == c.value.lower()

    @pytest.mark.parametrize("c", list(Classification))
    def test_round_trip_via_value(self, c):
        # Re-instantiating from value yields the same member.
        assert Classification(c.value) is c


class TestClassificationFromString:
    def test_known_alias_public(self):
        assert Classification.from_str("public") is Classification.PUBLIC

    def test_known_alias_uppercase(self):
        # Case-insensitive parse for operator ergonomics.
        assert Classification.from_str("PUBLIC") is Classification.PUBLIC
        assert Classification.from_str("Regulated") is Classification.REGULATED

    def test_unknown_raises_with_candidates(self):
        with pytest.raises(ValueError) as ei:
            Classification.from_str("nuclear")
        assert "candidates" in str(ei.value).lower() or "public" in str(ei.value)
