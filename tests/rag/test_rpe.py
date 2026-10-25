# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for Retrieval Policy Engine (#49, spec-rag-retrieval-policy.md).

RPE: (principal, intent, constraints) → RetrievalPlan.
Intent detection is model-mediated; the plan derivation from intent
is DETERMINISTIC (spec-classification-boundary §2 alignment).

Platform ships 8 core intents: Lookup, Diagnosis, Synthesis,
Teaching, Operations, Research, Generative, Metacognitive.
Extensions register domain-specific intents (symptom-triage,
incident-response, code-review, etc.).
"""

from __future__ import annotations

import pytest


class TestCoreIntents:
    def test_eight_core_intents_registered_by_default(self):
        from axiom.rag.rpe import CORE_INTENTS

        assert len(CORE_INTENTS) == 8
        expected = {
            "lookup", "diagnosis", "synthesis", "teaching",
            "operations", "research", "generative", "metacognitive",
        }
        assert {i.id for i in CORE_INTENTS} == expected

    def test_intent_dataclass_shape(self):
        from axiom.rag.rpe import Intent

        i = Intent(
            id="lookup",
            description="Factual retrieval; student needs a quick answer.",
            default_strategy="vector",
        )
        assert i.id == "lookup"
        assert i.default_strategy == "vector"


class TestIntentRegistry:
    def test_platform_intents_available_by_default(self):
        from axiom.rag.rpe import IntentRegistry

        reg = IntentRegistry.with_defaults()
        assert reg.get("lookup") is not None
        assert reg.get("teaching") is not None

    def test_extension_can_register_custom_intent(self):
        from axiom.rag.rpe import Intent, IntentRegistry

        reg = IntentRegistry.with_defaults()
        reg = reg.register(Intent(
            id="symptom-triage",
            description="Clinical symptom-to-diagnosis retrieval.",
            default_strategy="graph",
        ))
        assert reg.get("symptom-triage") is not None

    def test_duplicate_registration_raises(self):
        from axiom.rag.rpe import Intent, IntentRegistry

        reg = IntentRegistry.with_defaults()
        with pytest.raises(ValueError, match="already registered"):
            reg.register(Intent(
                id="lookup",  # already exists
                description="dup",
                default_strategy="vector",
            ))


class TestRetrievalPlan:
    def test_plan_shape(self):
        from axiom.rag.rpe import RetrievalPlan, SourceSpec

        plan = RetrievalPlan(
            sources=[SourceSpec(tier="org", cognitive_types=["semantic"])],
            strategy="vector",
            params={"top_k": 5, "maturity_floor": None},
        )
        assert len(plan.sources) == 1
        assert plan.strategy == "vector"
        assert plan.params["top_k"] == 5


class TestBuildPlanCoreIntents:
    """Each core intent produces a distinct, sensible plan."""

    def test_lookup_uses_vector_single_source(self):
        from axiom.rag.rpe import build_plan

        plan = build_plan(
            principal="@student:ut", intent_id="lookup",
            constraints={},
        )
        assert plan.strategy == "vector"
        assert plan.params["top_k"] <= 3

    def test_teaching_requires_maturity_floor_frameworks(self):
        """Students get Frameworks-tier or higher — pedagogy-ready."""
        from axiom.rag.rpe import build_plan

        plan = build_plan(
            principal="@student:ut", intent_id="teaching",
            constraints={},
        )
        assert plan.params.get("maturity_floor") == "Frameworks"

    def test_operations_restricts_to_facility_recency_biased(self):
        from axiom.rag.rpe import build_plan

        plan = build_plan(
            principal="@operator:example-host", intent_id="operations",
            constraints={},
        )
        # Operations prefers fresh data; bias toward recent
        assert plan.params.get("recency_bias", 0) > 0
        # Tier restriction for operations
        tiers = {s.tier for s in plan.sources}
        assert "org" in tiers or "facility" in tiers

    def test_research_is_broad_federated(self):
        from axiom.rag.rpe import build_plan

        plan = build_plan(
            principal="@researcher:ut", intent_id="research",
            constraints={"federated_peers": ["osu.edu", "inl.gov"]},
        )
        assert plan.strategy in ("hybrid", "fan_out")
        # Federation was permitted → fanned out
        assert any(s.remote for s in plan.sources)

    def test_synthesis_uses_multipack_multihop(self):
        from axiom.rag.rpe import build_plan

        plan = build_plan(
            principal="@researcher:ut", intent_id="synthesis",
            constraints={},
        )
        assert plan.strategy in ("graph", "hybrid")
        assert plan.params.get("hops", 1) > 1

    def test_diagnosis_uses_graph(self):
        from axiom.rag.rpe import build_plan

        plan = build_plan(
            principal="@engineer:example-host", intent_id="diagnosis",
            constraints={},
        )
        assert plan.strategy == "graph"

    def test_generative_output_first_params(self):
        """Generative = compose an artifact. Breadth over single-top-k."""
        from axiom.rag.rpe import build_plan

        plan = build_plan(
            principal="@student:ut", intent_id="generative",
            constraints={},
        )
        # Breadth for seed material, not top-1
        assert plan.params["top_k"] >= 5

    def test_metacognitive_pulls_user_trace_history(self):
        """Metacognitive = "how am I doing?" — pulls user's own history."""
        from axiom.rag.rpe import build_plan

        plan = build_plan(
            principal="@student:ut", intent_id="metacognitive",
            constraints={},
        )
        # Sources include the user's own traces, not just topic RAG
        assert any(s.source_type == "user_traces" for s in plan.sources)


class TestConstraintsShapeThePlan:
    def test_federated_peers_absent_means_local_only(self):
        from axiom.rag.rpe import build_plan

        plan = build_plan(
            principal="@r:ut", intent_id="research",
            constraints={},  # no peers
        )
        assert all(not s.remote for s in plan.sources)

    def test_classification_ceiling_restricts_tier(self):
        """EC constraint: refuse to fan out to peers lacking attestation."""
        from axiom.rag.rpe import build_plan

        plan = build_plan(
            principal="@r:ut", intent_id="research",
            constraints={
                "federated_peers": ["osu.edu"],
                "classification_ceiling": "EC",
                "peer_attestations": {},  # no peer is attested
            },
        )
        # No remote sources because no peer is attested
        assert all(not s.remote for s in plan.sources)


class TestExtensionIntentPlanOverride:
    def test_extension_intent_uses_its_default_strategy(self):
        from axiom.rag.rpe import Intent, IntentRegistry, build_plan_with_registry

        reg = IntentRegistry.with_defaults()
        reg = reg.register(Intent(
            id="incident-response",
            description="Emergency procedure retrieval under time pressure.",
            default_strategy="graph",
        ))
        plan = build_plan_with_registry(
            principal="@operator:example-host",
            intent_id="incident-response",
            constraints={},
            registry=reg,
        )
        assert plan.strategy == "graph"
        # Emergency → tightest top_k, maximum recency bias
        assert plan.params["top_k"] <= 3


class TestDeterminism:
    def test_same_inputs_same_plan(self):
        """Deterministic — same inputs always produce same plan."""
        from axiom.rag.rpe import build_plan

        args = dict(principal="@student:ut", intent_id="teaching",
                    constraints={"maturity_floor": "Frameworks"})
        plan1 = build_plan(**args)
        plan2 = build_plan(**args)
        assert plan1.strategy == plan2.strategy
        assert plan1.params == plan2.params
        assert [s.tier for s in plan1.sources] == [s.tier for s in plan2.sources]


class TestUnknownIntent:
    def test_unknown_intent_raises(self):
        from axiom.rag.rpe import build_plan

        with pytest.raises(ValueError, match="unknown intent"):
            build_plan(principal="@x", intent_id="made-up-intent",
                       constraints={})
