# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Retrieval Policy Engine (#49, spec-rag-retrieval-policy.md).

Maps `(principal, intent, constraints)` → `RetrievalPlan`.

Intent detection is model-mediated (LLM classifier); **plan
derivation from intent is DETERMINISTIC** — aligns with
spec-classification-boundary §2 (deterministic enforcement layers
even when upstream signals come from models).

Platform ships 8 core intents:
- lookup:         factual retrieval, top-1, low latency
- diagnosis:      troubleshoot / causal chain, graph traversal
- synthesis:      comprehension over multiple sources, multi-hop
- teaching:       pedagogy-ready, maturity floor = Frameworks
- operations:     facility-specific, recency-biased
- research:       breadth, federated fan-out when permitted
- generative:     seed material for artifact creation, breadth
- metacognitive:  "how am I doing", pulls user's own history

Extensions register domain-specific intents via `IntentRegistry`
(symptom-triage, incident-response, code-review, etc.). Domain
intents can also override the default decision table.

Future: CURIO Karpathy loop tunes the decision table from
observed outcomes. The current table encodes Ben's design
decisions from the separate-session RPE work; deviations that
improve measured quality feed back as proposals humans review.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Intent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Intent:
    """A named intent that shapes retrieval strategy.

    id: unique identifier (e.g. "lookup", "symptom-triage").
    description: human-readable note for docs / intent classifier.
    default_strategy: fallback strategy when no override matches.
    """

    id: str
    description: str
    default_strategy: str


# Platform-default intents. Extensions augment via IntentRegistry.
CORE_INTENTS = (
    Intent("lookup",        "Factual retrieval — quick answer.",           "vector"),
    Intent("diagnosis",     "Causal chain / troubleshooting.",             "graph"),
    Intent("synthesis",     "Multi-source comprehension.",                 "hybrid"),
    Intent("teaching",      "Pedagogy-ready, explanation-first.",          "vector"),
    Intent("operations",    "Facility-specific, fresh data.",              "vector"),
    Intent("research",      "Breadth; federation when permitted.",         "fan_out"),
    Intent("generative",    "Seed material for artifact creation.",        "vector"),
    Intent("metacognitive", "Reflection over user's own trace history.",   "trace"),
)


# ---------------------------------------------------------------------------
# IntentRegistry — immutable; extension registration returns new instance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntentRegistry:
    intents: tuple[Intent, ...] = ()

    @classmethod
    def with_defaults(cls) -> IntentRegistry:
        return cls(intents=CORE_INTENTS)

    def get(self, intent_id: str) -> Intent | None:
        for i in self.intents:
            if i.id == intent_id:
                return i
        return None

    def register(self, intent: Intent) -> IntentRegistry:
        if self.get(intent.id) is not None:
            raise ValueError(f"intent {intent.id!r} is already registered")
        return IntentRegistry(intents=tuple([*self.intents, intent]))


# ---------------------------------------------------------------------------
# RetrievalPlan output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceSpec:
    """Which pool of content to search.

    `source_type`: "rag" (standard indexed corpus), "user_traces"
    (the calling principal's own interaction history), or future
    extensions (e.g., "knowledge_graph").
    `tier`: "personal" | "org" | "community" | "facility" (for ops).
    `cognitive_types`: filter by MIRIX cognitive type (#42).
    `remote`: True iff this source is a federated peer.
    """

    source_type: str = "rag"
    tier: str | None = None
    cognitive_types: tuple[str, ...] = ()
    node: str | None = None   # populated when remote=True
    remote: bool = False


@dataclass(frozen=True)
class RetrievalPlan:
    sources: list[SourceSpec]
    strategy: str  # "vector" | "graph" | "hybrid" | "fan_out" | "trace"
    params: dict


# ---------------------------------------------------------------------------
# Decision table — one builder per intent
# ---------------------------------------------------------------------------


def _plan_lookup(constraints: dict) -> RetrievalPlan:
    return RetrievalPlan(
        sources=[SourceSpec(tier="org", cognitive_types=("semantic",))],
        strategy="vector",
        params={"top_k": 3, "maturity_floor": None},
    )


def _plan_diagnosis(constraints: dict) -> RetrievalPlan:
    return RetrievalPlan(
        sources=[SourceSpec(tier="org",
                            cognitive_types=("semantic", "procedural"))],
        strategy="graph",
        params={"top_k": 5, "hops": 2, "maturity_floor": "Facts"},
    )


def _plan_synthesis(constraints: dict) -> RetrievalPlan:
    return RetrievalPlan(
        sources=[
            SourceSpec(tier="org",
                       cognitive_types=("semantic", "procedural")),
            SourceSpec(tier="community", cognitive_types=("semantic",)),
        ],
        strategy="hybrid",
        params={"top_k": 10, "hops": 3, "maturity_floor": "Facts"},
    )


def _plan_teaching(constraints: dict) -> RetrievalPlan:
    return RetrievalPlan(
        sources=[SourceSpec(tier="org", cognitive_types=("semantic",))],
        strategy="vector",
        params={
            "top_k": 5,
            "maturity_floor": "Frameworks",
            "pedagogy_weight": 0.7,
        },
    )


def _plan_operations(constraints: dict) -> RetrievalPlan:
    return RetrievalPlan(
        sources=[SourceSpec(tier="org",
                            cognitive_types=("procedural", "episodic"))],
        strategy="vector",
        params={"top_k": 5, "recency_bias": 0.8, "maturity_floor": "Patterns"},
    )


def _plan_research(constraints: dict) -> RetrievalPlan:
    """Research plan fans out to federated peers when attested + permitted."""
    sources: list[SourceSpec] = [
        SourceSpec(tier="org",
                   cognitive_types=("semantic", "procedural")),
        SourceSpec(tier="community",
                   cognitive_types=("semantic", "procedural")),
    ]

    peers = constraints.get("federated_peers", [])
    ceiling = constraints.get("classification_ceiling")
    attestations = constraints.get("peer_attestations", {})

    for peer in peers:
        if ceiling == "EC" and peer not in attestations:
            # Refuse to fan out to an unattested peer under EC ceiling
            continue
        sources.append(SourceSpec(
            tier="community",
            node=peer,
            remote=True,
        ))

    strategy = "fan_out" if any(s.remote for s in sources) else "hybrid"
    return RetrievalPlan(
        sources=sources,
        strategy=strategy,
        params={"top_k": 20, "maturity_floor": None, "breadth_weight": 0.8},
    )


def _plan_generative(constraints: dict) -> RetrievalPlan:
    """Seed material for artifact creation — breadth over top-1."""
    return RetrievalPlan(
        sources=[SourceSpec(tier="org",
                            cognitive_types=("semantic", "resource"))],
        strategy="vector",
        params={"top_k": 10, "maturity_floor": None},
    )


def _plan_metacognitive(constraints: dict) -> RetrievalPlan:
    """Reflective intent — pull from the user's own trace history."""
    return RetrievalPlan(
        sources=[SourceSpec(source_type="user_traces")],
        strategy="trace",
        params={"top_k": 50, "time_window_days": 30},
    )


_CORE_PLAN_TABLE = {
    "lookup":        _plan_lookup,
    "diagnosis":     _plan_diagnosis,
    "synthesis":     _plan_synthesis,
    "teaching":      _plan_teaching,
    "operations":    _plan_operations,
    "research":      _plan_research,
    "generative":    _plan_generative,
    "metacognitive": _plan_metacognitive,
}


# ---------------------------------------------------------------------------
# Plan building
# ---------------------------------------------------------------------------


def build_plan(
    principal: str,
    intent_id: str,
    constraints: dict,
) -> RetrievalPlan:
    """Default plan builder — uses the platform's 8-intent decision table.

    For extension-registered intents, use `build_plan_with_registry`.
    """
    return build_plan_with_registry(
        principal=principal,
        intent_id=intent_id,
        constraints=constraints,
        registry=IntentRegistry.with_defaults(),
    )


def build_plan_with_registry(
    principal: str,
    intent_id: str,
    constraints: dict,
    registry: IntentRegistry,
) -> RetrievalPlan:
    """Plan builder that honors an IntentRegistry (platform + extensions).

    Core intents use the canonical decision table; extension intents
    fall back to a generic plan based on their `default_strategy`
    (tight top-k, single org source — conservative default).
    """
    intent = registry.get(intent_id)
    if intent is None:
        raise ValueError(f"unknown intent: {intent_id!r}")

    core_builder = _CORE_PLAN_TABLE.get(intent_id)
    if core_builder is not None:
        return core_builder(constraints)

    # Extension intent — use a sensible generic plan keyed on the
    # declared default_strategy. Tight top-k + single org source so
    # extensions don't accidentally get expensive defaults.
    return RetrievalPlan(
        sources=[SourceSpec(tier="org", cognitive_types=("semantic",))],
        strategy=intent.default_strategy,
        params={"top_k": 3, "recency_bias": 0.5},
    )
