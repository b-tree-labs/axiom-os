# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for trust graph (#47, ADR-028).

- TrustRecord: (trustor, target, context) + direct score
- TrustGraph: collection + queries
- Hierarchical resolution: explicit → role → node → institution → optimistic default
- EigenTrust-style derived trust (power iteration over the matrix)
- TrustContext definitions with user-tunable α + thresholds
- Adaptation-loop hook: record observation events
- Role-scoped records rebind under role succession
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# TrustRecord
# ---------------------------------------------------------------------------


class TestTrustRecord:
    def test_simple_record(self):
        from axiom.memory.ownership import TrustTarget
        from axiom.memory.trust import TrustRecord

        r = TrustRecord(
            trustor="@ben:ut",
            target=TrustTarget(principal="@alice:ut", role=None,
                               context="reactor-physics"),
            score=0.8,
        )
        assert r.trustor == "@ben:ut"
        assert r.target.principal == "@alice:ut"
        assert r.score == 0.8


# ---------------------------------------------------------------------------
# TrustContext
# ---------------------------------------------------------------------------


class TestTrustContext:
    def test_optimistic_defaults(self):
        from axiom.memory.trust import TrustContext

        ctx = TrustContext(id="general")
        # "Expect good behavior, tighten with emergent bad behavior"
        assert ctx.alpha_decay == 0.8
        assert ctx.admission_threshold == 0.3
        assert ctx.blast_radius_hops == 1

    def test_custom_context_parameters(self):
        from axiom.memory.trust import TrustContext

        ctx = TrustContext(
            id="reactor-physics/Facts/unclassified",
            alpha_decay=0.9,
            admission_threshold=0.5,
            blast_radius_hops=2,
        )
        assert ctx.alpha_decay == 0.9
        assert ctx.admission_threshold == 0.5


# ---------------------------------------------------------------------------
# TrustGraph basic ops
# ---------------------------------------------------------------------------


class TestGraphConstruction:
    def test_empty_graph_returns_optimistic_default(self):
        """Optimistic-with-adaptation: unknown principals get a welcoming
        baseline score matching the context's admission threshold."""
        from axiom.memory.trust import TrustContext, TrustGraph

        g = TrustGraph()
        ctx = TrustContext(id="general")
        score = g.direct_score("@ben:ut", "@alice:ut", ctx)
        # Optimistic default = the threshold itself (welcomes, doesn't boost)
        assert score == ctx.admission_threshold

    def test_record_direct_trust(self):
        from axiom.memory.ownership import TrustTarget
        from axiom.memory.trust import TrustContext, TrustGraph, TrustRecord

        g = TrustGraph()
        g = g.with_record(TrustRecord(
            trustor="@ben:ut",
            target=TrustTarget(principal="@alice:ut", role=None,
                               context="general"),
            score=0.9,
        ))
        assert g.direct_score("@ben:ut", "@alice:ut", TrustContext(id="general")) == 0.9


# ---------------------------------------------------------------------------
# Hierarchical resolution
# ---------------------------------------------------------------------------


class TestHierarchicalResolution:
    def test_explicit_human_beats_role(self):
        from axiom.memory.ownership import TrustTarget
        from axiom.memory.trust import TrustContext, TrustGraph, TrustRecord

        g = TrustGraph(role_membership={"@ut-faculty": {"@alice:ut"}})
        g = g.with_record(TrustRecord(
            trustor="@ben:ut",
            target=TrustTarget(principal=None, role="@ut-faculty",
                               context="general"),
            score=0.5,
        ))
        g = g.with_record(TrustRecord(
            trustor="@ben:ut",
            target=TrustTarget(principal="@alice:ut", role=None,
                               context="general"),
            score=0.9,
        ))
        score = g.resolve("@ben:ut", "@alice:ut", TrustContext(id="general"))
        assert score == 0.9  # explicit per-human beats role

    def test_role_fallback_when_no_human_record(self):
        from axiom.memory.ownership import TrustTarget
        from axiom.memory.trust import TrustContext, TrustGraph, TrustRecord

        g = TrustGraph(role_membership={"@ut-faculty": {"@alice:ut"}})
        g = g.with_record(TrustRecord(
            trustor="@ben:ut",
            target=TrustTarget(principal=None, role="@ut-faculty",
                               context="general"),
            score=0.75,
        ))
        score = g.resolve("@ben:ut", "@alice:ut", TrustContext(id="general"))
        assert score == 0.75

    def test_unknown_principal_falls_to_optimistic_default(self):
        from axiom.memory.trust import TrustContext, TrustGraph

        g = TrustGraph()
        ctx = TrustContext(id="general")
        score = g.resolve("@ben:ut", "@stranger:xx", ctx)
        assert score == ctx.admission_threshold


# ---------------------------------------------------------------------------
# Transitive trust (EigenTrust-style)
# ---------------------------------------------------------------------------


class TestTransitiveTrust:
    def test_one_hop_trust_propagates_with_decay(self):
        """A trusts B at 1.0; B trusts C at 1.0; A's derived trust of C = α."""
        from axiom.memory.ownership import TrustTarget
        from axiom.memory.trust import TrustContext, TrustGraph, TrustRecord

        ctx = TrustContext(id="general", alpha_decay=0.7,
                           admission_threshold=0.3, blast_radius_hops=1)
        g = TrustGraph()
        g = g.with_record(TrustRecord(
            trustor="@A", target=TrustTarget("@B", None, "general"), score=1.0,
        ))
        g = g.with_record(TrustRecord(
            trustor="@B", target=TrustTarget("@C", None, "general"), score=1.0,
        ))
        derived = g.derived_score("@A", "@C", ctx)
        # After 1 hop with α=0.7 → 0.7
        assert abs(derived - 0.7) < 1e-6

    def test_two_hop_trust_compounds(self):
        from axiom.memory.ownership import TrustTarget
        from axiom.memory.trust import TrustContext, TrustGraph, TrustRecord

        ctx = TrustContext(id="general", alpha_decay=0.5,
                           admission_threshold=0.3, blast_radius_hops=3)
        g = TrustGraph()
        for edge in [("@A", "@B"), ("@B", "@C"), ("@C", "@D")]:
            g = g.with_record(TrustRecord(
                trustor=edge[0],
                target=TrustTarget(edge[1], None, "general"),
                score=1.0,
            ))
        # Two hops: α² = 0.25
        derived = g.derived_score("@A", "@D", ctx)
        # Allow some slack for iteration convergence
        assert derived < 0.3 and derived > 0.05

    def test_proximity_boosts_with_multiple_paths(self):
        """Two independent trust paths → higher derived trust than one."""
        from axiom.memory.ownership import TrustTarget
        from axiom.memory.trust import TrustContext, TrustGraph, TrustRecord

        ctx = TrustContext(id="general", alpha_decay=0.5,
                           admission_threshold=0.3, blast_radius_hops=2)
        single = TrustGraph()
        for edge in [("@A", "@B"), ("@B", "@C")]:
            single = single.with_record(TrustRecord(
                trustor=edge[0],
                target=TrustTarget(edge[1], None, "general"),
                score=1.0,
            ))
        multi = single
        # Add a second path A→X→C
        for edge in [("@A", "@X"), ("@X", "@C")]:
            multi = multi.with_record(TrustRecord(
                trustor=edge[0],
                target=TrustTarget(edge[1], None, "general"),
                score=1.0,
            ))
        single_derived = single.derived_score("@A", "@C", ctx)
        multi_derived = multi.derived_score("@A", "@C", ctx)
        assert multi_derived > single_derived


# ---------------------------------------------------------------------------
# Adaptation loop hooks
# ---------------------------------------------------------------------------


class TestAdaptationLoop:
    def test_record_observation_event(self):
        from axiom.memory.trust import ObservationEvent, TrustGraph

        g = TrustGraph()
        g = g.record_observation(ObservationEvent(
            observer="@ben:ut",
            subject="@alice:ut",
            kind="breach_detected",
            weight=1.0,
            at="2026-04-17T10:00:00Z",
        ))
        observations = g.observations_for(subject="@alice:ut")
        assert len(observations) == 1
        assert observations[0].kind == "breach_detected"

    def test_breach_observations_push_toward_demotion(self):
        """Not mutation — the adaptation loop reads observations and
        emits adjustment proposals, which humans review."""
        from axiom.memory.trust import (
            ObservationEvent,
            TrustGraph,
            propose_adjustments,
        )

        g = TrustGraph()
        for _ in range(5):
            g = g.record_observation(ObservationEvent(
                observer="@ben:ut",
                subject="@alice:ut",
                kind="breach_detected",
                weight=1.0,
                at="2026-04-17T10:00:00Z",
            ))
        proposals = propose_adjustments(g, subject="@alice:ut")
        # Multiple breaches → proposed demotion
        assert len(proposals) > 0
        assert proposals[0]["direction"] == "down"


# ---------------------------------------------------------------------------
# Role succession rebinds role-scoped records
# ---------------------------------------------------------------------------


class TestRoleSuccession:
    def test_succession_rebinds_role_records(self):
        """Role-scoped trust follows the role to its new occupant.
        Human-scoped trust stays with the outgoing human."""
        from axiom.memory.ownership import TrustTarget, role_succession
        from axiom.memory.trust import (
            TrustContext,
            TrustGraph,
            TrustRecord,
            apply_succession,
        )

        g = TrustGraph(role_membership={
            "@ut-nuclear-chair": {"@alice:ut"},
        })
        # Role-scoped record
        g = g.with_record(TrustRecord(
            trustor="@ben:ut",
            target=TrustTarget(principal=None, role="@ut-nuclear-chair",
                               context="general"),
            score=0.85,
        ))
        # Human-scoped record (should NOT move)
        g = g.with_record(TrustRecord(
            trustor="@ben:ut",
            target=TrustTarget(principal="@alice:ut", role=None,
                               context="general"),
            score=0.95,
        ))

        # Succession: Alice → Bob
        succession = role_succession(
            role="@ut-nuclear-chair",
            outgoing_principal="@alice:ut",
            incoming_principal="@bob:ut",
            outgoing_signature=b"alice-stepping-down",
            incoming_signature=b"bob-accepts",
            effective_at="2026-06-01T00:00:00Z",
        )
        g = apply_succession(g, succession)

        # Bob inherits role trust via role membership
        ctx = TrustContext(id="general")
        role_score_bob = g.resolve("@ben:ut", "@bob:ut", ctx)
        assert role_score_bob == 0.85

        # Alice keeps her human-scoped trust
        alice_score = g.resolve("@ben:ut", "@alice:ut", ctx)
        assert alice_score == 0.95


# ---------------------------------------------------------------------------
# Privacy — records are private to the trustor
# ---------------------------------------------------------------------------


class TestPrivacy:
    def test_view_filtered_to_trustor(self):
        """Alice can't see Ben's private trust records for her."""
        from axiom.memory.ownership import TrustTarget
        from axiom.memory.trust import TrustGraph, TrustRecord

        g = TrustGraph()
        g = g.with_record(TrustRecord(
            trustor="@ben:ut",
            target=TrustTarget("@alice:ut", None, "general"),
            score=0.3,
        ))
        # Alice viewing the graph sees no records (trust is private)
        visible = g.records_visible_to("@alice:ut")
        assert visible == ()

        # Ben sees his own
        visible_ben = g.records_visible_to("@ben:ut")
        assert len(visible_ben) == 1
