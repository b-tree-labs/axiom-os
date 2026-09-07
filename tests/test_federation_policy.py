# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for federation-policy primitives.

Per `spec-federation-policy.md`:

- VisibilityHorizon enum with stable ordering for ``min()`` composition
- Alias resolution that maps either abstract horizon names or
  extension-declared aliases to the underlying enum
- TrustProfile with conservative defaults (default-deny on every
  outflow + inflow dimension)
"""

from __future__ import annotations

import pytest

from axiom.vega.federation.policy import (
    ClassificationStamp,
    ExportControl,
    InboundOverride,
    ProprietaryRestriction,
    TrustProfile,
    VisibilityHorizon,
    default_trust_profile,
    resolve_visibility,
)

# ---------------------------------------------------------------------------
# VisibilityHorizon — ordering + composition
# ---------------------------------------------------------------------------


class TestVisibilityHorizon:
    def test_levels_are_strictly_ordered(self):
        assert VisibilityHorizon.SCOPE_INTERNAL.level == 0
        assert VisibilityHorizon.REQUEST_GATED.level == 1
        assert VisibilityHorizon.PEERS_DECLARED.level == 2
        assert VisibilityHorizon.FEDERATION_BOUND.level == 3
        assert VisibilityHorizon.PUBLIC.level == 4

    def test_most_restrictive_picks_lowest_level(self):
        result = VisibilityHorizon.most_restrictive(
            VisibilityHorizon.PUBLIC,
            VisibilityHorizon.PEERS_DECLARED,
        )
        assert result is VisibilityHorizon.PEERS_DECLARED

    def test_most_restrictive_with_one_input_returns_it(self):
        result = VisibilityHorizon.most_restrictive(VisibilityHorizon.FEDERATION_BOUND)
        assert result is VisibilityHorizon.FEDERATION_BOUND

    def test_most_restrictive_empty_input_is_default_deny(self):
        """Empty input → SCOPE_INTERNAL. Default-deny is the design posture."""
        assert (
            VisibilityHorizon.most_restrictive() is VisibilityHorizon.SCOPE_INTERNAL
        )

    def test_serializes_as_string_value(self):
        """String enum so JSON / TOML serialize without custom encoders."""
        assert VisibilityHorizon.SCOPE_INTERNAL.value == "scope_internal"
        assert VisibilityHorizon.PUBLIC.value == "public"


# ---------------------------------------------------------------------------
# Alias resolution
# ---------------------------------------------------------------------------


class TestResolveVisibility:
    def test_passthrough_for_enum_value(self):
        assert (
            resolve_visibility(VisibilityHorizon.PEERS_DECLARED)
            is VisibilityHorizon.PEERS_DECLARED
        )

    def test_resolves_canonical_string(self):
        assert (
            resolve_visibility("federation_bound")
            is VisibilityHorizon.FEDERATION_BOUND
        )

    def test_resolves_extension_alias(self):
        """Classroom's `cohort-private` aliases to SCOPE_INTERNAL."""
        aliases = {
            "cohort-private": "scope_internal",
            "cohort-shared": "peers_declared",
            "public-curated": "public",
        }
        assert (
            resolve_visibility("cohort-private", aliases=aliases)
            is VisibilityHorizon.SCOPE_INTERNAL
        )
        assert (
            resolve_visibility("public-curated", aliases=aliases)
            is VisibilityHorizon.PUBLIC
        )

    def test_alias_to_unknown_horizon_raises(self):
        aliases = {"weird": "not_a_real_horizon"}
        with pytest.raises(ValueError, match="weird.*not_a_real_horizon"):
            resolve_visibility("weird", aliases=aliases)

    def test_unknown_value_raises_with_candidates(self):
        with pytest.raises(ValueError, match="unknown visibility"):
            resolve_visibility("nonsense")

    def test_unknown_value_with_aliases_lists_both_sources(self):
        aliases = {"cohort-private": "scope_internal"}
        with pytest.raises(ValueError) as exc:
            resolve_visibility("nonsense", aliases=aliases)
        msg = str(exc.value)
        assert "scope_internal" in msg
        assert "cohort-private" in msg


# ---------------------------------------------------------------------------
# TrustProfile — defaults + invariants
# ---------------------------------------------------------------------------


class TestTrustProfileDefaults:
    def test_default_is_fully_isolated(self):
        """Default profile is default-deny on every dimension."""
        p = default_trust_profile("ne101-prague")
        assert p.scope == "ne101-prague"
        assert p.declared_peers == frozenset()
        assert p.federation_max_hops == 1
        assert p.public_discoverable is False
        assert p.inbound_horizons == frozenset(
            {VisibilityHorizon.SCOPE_INTERNAL}
        )
        assert p.inbound_classification_max == "unclassified"
        assert p.inbound_per_peer == {}
        assert p.prefer_concepts_over_full is True

    def test_negative_max_hops_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            TrustProfile(scope="x", federation_max_hops=-1)

    def test_max_hops_greater_than_two_rejected(self):
        """Spec §5: hops > 2 requires explicit policy beyond the default."""
        with pytest.raises(ValueError, match="explicit policy"):
            TrustProfile(scope="x", federation_max_hops=3)

    def test_inbound_per_peer_override_smoke(self):
        """A scope can declare a more restrictive override for a specific peer."""
        override = InboundOverride(
            accepted_horizons=frozenset({VisibilityHorizon.SCOPE_INTERNAL}),
            classification_max="unclassified",
        )
        p = TrustProfile(
            scope="x",
            inbound_horizons=frozenset(
                {VisibilityHorizon.SCOPE_INTERNAL, VisibilityHorizon.PEERS_DECLARED}
            ),
            inbound_per_peer={"@suspicious-peer": override},
        )
        assert p.inbound_per_peer["@suspicious-peer"].accepted_horizons == frozenset(
            {VisibilityHorizon.SCOPE_INTERNAL}
        )

    def test_can_declare_peers_explicitly(self):
        p = TrustProfile(
            scope="x",
            declared_peers=frozenset({"@partner-1", "@partner-2"}),
        )
        assert "@partner-1" in p.declared_peers
        assert "@partner-2" in p.declared_peers


# ---------------------------------------------------------------------------
# ClassificationStamp — regulatory ceiling on outflow
# ---------------------------------------------------------------------------


class TestClassificationStampDefaults:
    def test_unclassified_factory_is_fully_default(self):
        stamp = ClassificationStamp.unclassified()
        assert stamp.level == "unclassified"
        assert stamp.compartments == frozenset()
        assert stamp.export_control.itar is False
        assert stamp.export_control.ear_categories == frozenset()
        assert stamp.proprietary.restricted is False

    def test_unclassified_allows_public_outflow(self):
        """The least-restrictive stamp permits all the way to PUBLIC.
        Visibility default-deny stops outflow at SCOPE_INTERNAL anyway."""
        stamp = ClassificationStamp.unclassified()
        assert stamp.allowed_outflow_level() is VisibilityHorizon.PUBLIC


class TestClassificationOutflowCeiling:
    """Validate the v0 mapping from regimes to outflow horizon."""

    def test_cui_caps_at_peers_declared(self):
        stamp = ClassificationStamp(level="cui")
        assert stamp.allowed_outflow_level() is VisibilityHorizon.PEERS_DECLARED

    def test_secret_caps_at_scope_internal(self):
        stamp = ClassificationStamp(level="secret")
        assert stamp.allowed_outflow_level() is VisibilityHorizon.SCOPE_INTERNAL

    def test_top_secret_caps_at_scope_internal(self):
        stamp = ClassificationStamp(level="top_secret")
        assert stamp.allowed_outflow_level() is VisibilityHorizon.SCOPE_INTERNAL

    def test_unknown_level_defaults_to_scope_internal(self):
        """Defensive default — an unrecognized level is treated as
        most-restrictive rather than silently permitted."""
        stamp = ClassificationStamp(level="🤷")
        assert stamp.allowed_outflow_level() is VisibilityHorizon.SCOPE_INTERNAL

    def test_compartments_force_scope_internal(self):
        """Any SCI compartment marking forces SCOPE_INTERNAL regardless of level."""
        stamp = ClassificationStamp(
            level="unclassified",
            compartments=frozenset({"NOFORN"}),
        )
        assert stamp.allowed_outflow_level() is VisibilityHorizon.SCOPE_INTERNAL

    def test_itar_caps_at_peers_declared(self):
        stamp = ClassificationStamp(
            level="unclassified",
            export_control=ExportControl(itar=True),
        )
        assert stamp.allowed_outflow_level() is VisibilityHorizon.PEERS_DECLARED

    def test_ear_caps_at_peers_declared(self):
        stamp = ClassificationStamp(
            level="unclassified",
            export_control=ExportControl(
                ear_categories=frozenset({"0E982"}),
                ear_authorized_nationalities=frozenset({"US"}),
            ),
        )
        assert stamp.allowed_outflow_level() is VisibilityHorizon.PEERS_DECLARED

    def test_part_810_caps_at_request_gated(self):
        stamp = ClassificationStamp(
            level="unclassified",
            export_control=ExportControl(part_810_applicable=True),
        )
        assert stamp.allowed_outflow_level() is VisibilityHorizon.REQUEST_GATED

    def test_proprietary_caps_at_request_gated(self):
        stamp = ClassificationStamp(
            level="unclassified",
            proprietary=ProprietaryRestriction(restricted=True, license="NDA-42"),
        )
        assert stamp.allowed_outflow_level() is VisibilityHorizon.REQUEST_GATED

    def test_multiple_constraints_take_most_restrictive(self):
        """CUI + ITAR + Part 810 + proprietary all apply; the most
        restrictive wins."""
        stamp = ClassificationStamp(
            level="cui",                           # ceiling = PEERS_DECLARED
            export_control=ExportControl(
                itar=True,                          # cap = PEERS_DECLARED
                part_810_applicable=True,           # cap = REQUEST_GATED
            ),
            proprietary=ProprietaryRestriction(restricted=True),  # cap = REQUEST_GATED
        )
        assert stamp.allowed_outflow_level() is VisibilityHorizon.REQUEST_GATED

    def test_compartments_dominate_other_regimes(self):
        """SCI compartments + EAR + proprietary still collapse to SCOPE_INTERNAL."""
        stamp = ClassificationStamp(
            level="cui",
            compartments=frozenset({"NOFORN"}),
            export_control=ExportControl(itar=True),
            proprietary=ProprietaryRestriction(restricted=True),
        )
        assert stamp.allowed_outflow_level() is VisibilityHorizon.SCOPE_INTERNAL


class TestClassificationStampSerialization:
    def test_unclassified_round_trip(self):
        original = ClassificationStamp.unclassified()
        decoded = ClassificationStamp.from_dict(original.to_dict())
        assert decoded == original

    def test_full_stamp_round_trip(self):
        original = ClassificationStamp(
            level="cui",
            compartments=frozenset({"NOFORN", "FOUO"}),
            export_control=ExportControl(
                itar=True,
                ear_categories=frozenset({"0E982", "1A983"}),
                ear_authorized_nationalities=frozenset({"US", "CA"}),
                part_810_applicable=True,
                part_810_specific_authorization="DOE-AUTH-12345",
            ),
            proprietary=ProprietaryRestriction(
                restricted=True,
                license="contract-XYZ-2026",
            ),
            original_classifier="@officer:doe",
            classification_date="2026-04-25T00:00:00+00:00",
            declassification_date="2046-04-25T00:00:00+00:00",
        )
        decoded = ClassificationStamp.from_dict(original.to_dict())
        assert decoded == original

    def test_legacy_dict_decodes_to_unclassified(self):
        """Empty dict + missing fields should default to fully-unclassified."""
        decoded = ClassificationStamp.from_dict({})
        assert decoded == ClassificationStamp.unclassified()

    def test_partial_dict_fills_missing_fields(self):
        """Partially-specified dicts (e.g., older stored fragments) decode
        with defaults for the missing pieces."""
        decoded = ClassificationStamp.from_dict({"level": "cui"})
        assert decoded.level == "cui"
        assert decoded.compartments == frozenset()
        assert decoded.export_control.itar is False
