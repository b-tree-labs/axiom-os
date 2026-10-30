# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for axiom/memory/fragment.py — MemoryFragment primitive.

Per Collaborative Memory paper (arXiv 2505.18279) §3.2:
  Immutable provenance tuple (T, U, A, R):
  - T(m): creation timestamp
  - U(m): contributing user (principal)
  - A(m): contributing agents (set)
  - R(m): resources accessed during creation (set)

Per MIRIX (Substrate-App) 6-manager cognitive-type taxonomy:
  core | episodic | semantic | procedural | resource | vault

Fragments are write-once. ACL changes and federation trust state
changes NEVER mutate a fragment — they mutate the access graphs
evaluated at read time (task #34).
"""

from __future__ import annotations

import pytest


class TestCognitiveTypes:
    def test_all_six_types_defined(self):
        from axiom.memory.fragment import CognitiveType

        assert CognitiveType.CORE.value == "core"
        assert CognitiveType.EPISODIC.value == "episodic"
        assert CognitiveType.SEMANTIC.value == "semantic"
        assert CognitiveType.PROCEDURAL.value == "procedural"
        assert CognitiveType.RESOURCE.value == "resource"
        assert CognitiveType.VAULT.value == "vault"

    def test_from_string(self):
        from axiom.memory.fragment import CognitiveType

        assert CognitiveType.from_string("episodic") == CognitiveType.EPISODIC

    def test_unknown_string_raises(self):
        from axiom.memory.fragment import CognitiveType

        with pytest.raises(ValueError, match="unknown cognitive type"):
            CognitiveType.from_string("made-up-type")


class TestFragmentCreation:
    def test_create_basic_fragment(self):
        from axiom.memory.fragment import CognitiveType, create_fragment

        frag = create_fragment(
            content={"fact": "fission splits heavy nuclei"},
            cognitive_type="semantic",
            principal_id="ben@ut.edu",
            agents={"axi"},
            resources={"rag-org"},
        )

        assert frag.id  # uuid auto-generated
        assert frag.cognitive_type == CognitiveType.SEMANTIC
        assert frag.content == {"fact": "fission splits heavy nuclei"}
        assert frag.provenance.principal_id == "ben@ut.edu"
        assert frag.provenance.agents == frozenset({"axi"})
        assert frag.provenance.resources == frozenset({"rag-org"})
        assert frag.provenance.timestamp  # ISO 8601 string

    def test_agents_and_resources_can_be_empty(self):
        from axiom.memory.fragment import create_fragment

        # e.g. a user-authored note with no agent or external resource
        frag = create_fragment(
            content={"note": "meeting takeaway", "event_time": "2026-04-17T10:00:00Z"},
            cognitive_type="episodic",
            principal_id="u1",
            agents=set(),
            resources=set(),
        )
        assert frag.provenance.agents == frozenset()
        assert frag.provenance.resources == frozenset()


class TestImmutability:
    """Fragments are write-once — provenance never mutates."""

    def test_fragment_is_frozen(self):
        from axiom.memory.fragment import create_fragment

        frag = create_fragment(
            content={"x": 1}, cognitive_type="semantic",
            principal_id="u1", agents={"a1"}, resources={"r1"},
        )
        with pytest.raises(Exception):  # noqa: B017  # dataclasses.FrozenInstanceError
            frag.content = {"x": 2}

    def test_provenance_is_frozen(self):
        from axiom.memory.fragment import create_fragment

        frag = create_fragment(
            content={"x": 1}, cognitive_type="semantic",
            principal_id="u1", agents={"a1"}, resources={"r1"},
        )
        with pytest.raises(Exception):  # noqa: B017
            frag.provenance.principal_id = "someone_else"


class TestContentValidation:
    """Each cognitive type has minimal content-shape expectations."""

    def test_procedural_must_have_steps(self):
        from axiom.memory.fragment import create_fragment

        with pytest.raises(ValueError, match="procedural.*steps"):
            create_fragment(
                content={"no_steps": "here"},
                cognitive_type="procedural",
                principal_id="u1", agents=set(), resources=set(),
            )

    def test_procedural_with_steps_succeeds(self):
        from axiom.memory.fragment import create_fragment

        frag = create_fragment(
            content={"workflow": "deploy", "steps": ["checkout", "test", "push"]},
            cognitive_type="procedural",
            principal_id="u1", agents=set(), resources=set(),
        )
        assert frag.content["steps"] == ["checkout", "test", "push"]

    def test_resource_must_have_ref(self):
        from axiom.memory.fragment import create_fragment

        with pytest.raises(ValueError, match="resource.*ref"):
            create_fragment(
                content={"no_ref": "x"},
                cognitive_type="resource",
                principal_id="u1", agents=set(), resources=set(),
            )

    def test_episodic_requires_event_time(self):
        from axiom.memory.fragment import create_fragment

        with pytest.raises(ValueError, match="episodic.*event_time"):
            create_fragment(
                content={"no_time": "x"},
                cognitive_type="episodic",
                principal_id="u1", agents=set(), resources=set(),
            )


class TestSerializationRoundTrip:
    def test_to_dict_and_back(self):
        from axiom.memory.fragment import (
            create_fragment,
            fragment_from_dict,
        )

        original = create_fragment(
            content={"fact": "x"},
            cognitive_type="semantic",
            principal_id="u1", agents={"a1", "a2"}, resources={"r1"},
        )
        as_dict = original.to_dict()

        # Dict shape is JSON-safe (sets → sorted lists, frozensets likewise)
        assert as_dict["cognitive_type"] == "semantic"
        assert sorted(as_dict["provenance"]["agents"]) == ["a1", "a2"]
        assert as_dict["provenance"]["resources"] == ["r1"]

        restored = fragment_from_dict(as_dict)
        assert restored == original


class TestRetentionDefault:
    def test_defaults_to_active_tier(self):
        from axiom.memory.fragment import RetentionTier, create_fragment

        frag = create_fragment(
            content={"x": 1}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        assert frag.retention_tier == RetentionTier.ACTIVE
        assert frag.ttl is None  # no expiry by default


class TestProvenanceTupleView:
    """The (T, U, A, R) tuple is the paper's core operator input."""

    def test_tuple_view(self):
        from axiom.memory.fragment import create_fragment

        frag = create_fragment(
            content={"x": 1}, cognitive_type="semantic",
            principal_id="u1", agents={"a1"}, resources={"r1", "r2"},
        )
        t, u, a, r = frag.provenance.as_tuple()
        assert isinstance(t, str)  # ISO 8601
        assert u == "u1"
        assert a == frozenset({"a1"})
        assert r == frozenset({"r1", "r2"})


# ---------------------------------------------------------------------------
# Visibility horizon — federation-policy field on MemoryFragment
# (per ADR-033 + spec-federation-policy.md)
# ---------------------------------------------------------------------------


class TestVisibilityHorizonField:
    """The `visibility` field on MemoryFragment carries the writer's
    per-fragment outflow intent. Default is SCOPE_INTERNAL (default-deny)
    so any fragment created before the writer thinks about visibility
    stays put."""

    def test_default_is_scope_internal(self):
        from axiom.memory.fragment import create_fragment
        from axiom.vega.federation.policy import VisibilityHorizon

        frag = create_fragment(
            content={"x": 1}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        assert frag.visibility is VisibilityHorizon.SCOPE_INTERNAL

    def test_can_construct_with_explicit_visibility(self):
        """Higher horizons set explicitly at construction."""
        import dataclasses

        from axiom.memory.fragment import create_fragment
        from axiom.vega.federation.policy import VisibilityHorizon

        base = create_fragment(
            content={"x": 1}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        promoted = dataclasses.replace(
            base, visibility=VisibilityHorizon.PEERS_DECLARED,
        )
        assert promoted.visibility is VisibilityHorizon.PEERS_DECLARED
        # And the original is unchanged (immutability invariant).
        assert base.visibility is VisibilityHorizon.SCOPE_INTERNAL

    def test_to_dict_includes_visibility_value(self):
        from axiom.memory.fragment import create_fragment
        from axiom.vega.federation.policy import VisibilityHorizon

        frag = create_fragment(
            content={"x": 1}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        d = frag.to_dict()
        assert d["visibility"] == VisibilityHorizon.SCOPE_INTERNAL.value

    def test_round_trip_preserves_visibility(self):
        """to_dict + fragment_from_dict round-trip is stable."""
        import dataclasses

        from axiom.memory.fragment import create_fragment, fragment_from_dict
        from axiom.vega.federation.policy import VisibilityHorizon

        base = create_fragment(
            content={"x": 1}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        promoted = dataclasses.replace(
            base, visibility=VisibilityHorizon.PUBLIC,
        )
        decoded = fragment_from_dict(promoted.to_dict())
        assert decoded.visibility is VisibilityHorizon.PUBLIC

    def test_decoding_legacy_dict_without_visibility_defaults_to_internal(self):
        """Backward-compat: a dict missing the visibility key decodes with
        the default SCOPE_INTERNAL — no migration required for existing
        on-disk fragments."""
        from axiom.memory.fragment import create_fragment, fragment_from_dict
        from axiom.vega.federation.policy import VisibilityHorizon

        frag = create_fragment(
            content={"x": 1}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        d = frag.to_dict()
        del d["visibility"]   # simulate a pre-VisibilityHorizon record on disk
        decoded = fragment_from_dict(d)
        assert decoded.visibility is VisibilityHorizon.SCOPE_INTERNAL


class TestClassificationStampField:
    """`classification` is the regulatory constraint that the federation
    gateway pairs with `visibility` when deciding outflow. Default is
    fully-unclassified — extensions that don't think about classification
    get a stamp that lets visibility do the gating (visibility itself
    defaults to SCOPE_INTERNAL, so combined effective outflow stays
    default-deny)."""

    def test_default_is_unclassified(self):
        from axiom.memory.fragment import create_fragment
        from axiom.vega.federation.policy import ClassificationStamp

        frag = create_fragment(
            content={"x": 1}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        assert frag.classification == ClassificationStamp.unclassified()
        assert frag.classification.level == "unclassified"

    def test_can_construct_with_explicit_classification(self):
        import dataclasses

        from axiom.memory.fragment import create_fragment
        from axiom.vega.federation.policy import (
            ClassificationStamp,
            ExportControl,
        )

        base = create_fragment(
            content={"x": 1}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        classified = dataclasses.replace(
            base,
            classification=ClassificationStamp(
                level="cui",
                export_control=ExportControl(part_810_applicable=True),
                original_classifier="@officer:doe",
                classification_date="2026-04-25T00:00:00+00:00",
            ),
        )
        assert classified.classification.level == "cui"
        assert classified.classification.export_control.part_810_applicable is True
        # Original is untouched (immutability invariant).
        assert base.classification.level == "unclassified"

    def test_to_dict_includes_classification_dict(self):
        from axiom.memory.fragment import create_fragment

        frag = create_fragment(
            content={"x": 1}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        d = frag.to_dict()
        assert "classification" in d
        assert d["classification"]["level"] == "unclassified"

    def test_round_trip_preserves_classification(self):
        import dataclasses

        from axiom.memory.fragment import create_fragment, fragment_from_dict
        from axiom.vega.federation.policy import (
            ClassificationStamp,
            ProprietaryRestriction,
        )

        base = create_fragment(
            content={"x": 1}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        stamped = dataclasses.replace(
            base,
            classification=ClassificationStamp(
                level="unclassified",
                proprietary=ProprietaryRestriction(
                    restricted=True, license="NDA-2026",
                ),
                original_classifier="@officer:bbooth",
                classification_date="2026-04-25T12:00:00+00:00",
            ),
        )
        decoded = fragment_from_dict(stamped.to_dict())
        assert decoded.classification == stamped.classification

    def test_decoding_legacy_dict_without_classification_defaults_unclassified(self):
        from axiom.memory.fragment import create_fragment, fragment_from_dict
        from axiom.vega.federation.policy import ClassificationStamp

        frag = create_fragment(
            content={"x": 1}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        d = frag.to_dict()
        del d["classification"]
        decoded = fragment_from_dict(d)
        assert decoded.classification == ClassificationStamp.unclassified()


class TestEffectiveOutflowComposition:
    """Federation gateway will compute effective outflow as
    min(visibility, classification.allowed_outflow_level()).
    These tests pin the per-fragment composition at the data level."""

    def test_unclassified_with_public_visibility_stays_public(self):
        import dataclasses

        from axiom.memory.fragment import create_fragment
        from axiom.vega.federation.policy import VisibilityHorizon

        frag = dataclasses.replace(
            create_fragment(
                content={"x": 1}, cognitive_type="semantic",
                principal_id="u1", agents=set(), resources=set(),
            ),
            visibility=VisibilityHorizon.PUBLIC,
        )
        effective = VisibilityHorizon.most_restrictive(
            frag.visibility,
            frag.classification.allowed_outflow_level(),
        )
        assert effective is VisibilityHorizon.PUBLIC

    def test_cui_classification_collapses_optimistic_public(self):
        """The classic 'classification trumps visibility' case."""
        import dataclasses

        from axiom.memory.fragment import create_fragment
        from axiom.vega.federation.policy import (
            ClassificationStamp,
            VisibilityHorizon,
        )

        frag = dataclasses.replace(
            create_fragment(
                content={"x": 1}, cognitive_type="semantic",
                principal_id="u1", agents=set(), resources=set(),
            ),
            visibility=VisibilityHorizon.PUBLIC,
            classification=ClassificationStamp(level="cui"),
        )
        effective = VisibilityHorizon.most_restrictive(
            frag.visibility,
            frag.classification.allowed_outflow_level(),
        )
        assert effective is VisibilityHorizon.PEERS_DECLARED

    def test_secret_collapses_to_scope_internal(self):
        import dataclasses

        from axiom.memory.fragment import create_fragment
        from axiom.vega.federation.policy import (
            ClassificationStamp,
            VisibilityHorizon,
        )

        frag = dataclasses.replace(
            create_fragment(
                content={"x": 1}, cognitive_type="semantic",
                principal_id="u1", agents=set(), resources=set(),
            ),
            visibility=VisibilityHorizon.PUBLIC,
            classification=ClassificationStamp(level="secret"),
        )
        effective = VisibilityHorizon.most_restrictive(
            frag.visibility,
            frag.classification.allowed_outflow_level(),
        )
        assert effective is VisibilityHorizon.SCOPE_INTERNAL
