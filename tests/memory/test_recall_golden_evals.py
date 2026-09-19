# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Golden recall evals + latency budget (cross-mem P1 acceptance gate).

Each eval pairs a stored memory with a paraphrased query that is NOT a
substring of the stored content — the structured-filtering probe (the
same case-insensitive substring match the forget skill uses) must MISS
it, while recall() must place the target in the top-k. Both sides are
asserted, so the eval demonstrates the capability gap recall closes
(PRD F2).

Runs deterministically FTS-only (no embedder): if lexical fusion alone
answers these, hybrid can only improve. Latency: p95 over the eval
queries must stay inside the working budget (500 ms local — see
docs/working/cross-mem-p1-open-questions.md; pending a pinned number).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

PRINCIPAL = "@alice:evals"
AGENT = "axi"
P95_BUDGET_SECONDS = 0.5

# (key, cognitive_type, content) — the seeded memory corpus.
_CORPUS = [
    ("coffee", "semantic", {"fact": "prefers dark roast coffee from the campus cart"}),
    ("tabs", "semantic", {"fact": "indentation style is tabs, never spaces"}),
    ("standup", "episodic", {"summary": "moved the team standup to 09:30 on Mondays",
                             "event_time": "2026-06-02T09:30:00+00:00"}),
    ("laptop", "resource", {"ref": "asset://laptops/mbp-2031", "name": "silver laptop",
                            "description": "loaner laptop returned to IT in June"}),
    ("deploy", "procedural", {"steps": ["freeze main", "tag release", "run canary"],
                              "summary": "release procedure for the web service"}),
    ("thesis", "semantic", {"fact": "dissertation defense scheduled for December"}),
    ("budget", "episodic", {"summary": "quarterly budget review flagged a travel overrun",
                            "event_time": "2026-05-15T14:00:00+00:00"}),
    ("parking", "semantic", {"fact": "parking garage code is stored in the wallet card"}),
    ("mentor", "semantic", {"fact": "weekly mentoring slot with Jordan on Thursdays"}),
    ("diet", "core", {"persona": "vegetarian; allergic to shellfish"}),
]

# (query, target_key) — paraphrases sharing tokens but never substrings.
_GOLDEN = [
    ("what kind of coffee does she drink", "coffee"),
    ("tabs or spaces for indentation", "tabs"),
    ("when did the standup time change", "standup"),
    ("which laptop went back to IT", "laptop"),
    ("how do we run a release canary", "deploy"),
    ("when is the dissertation defense", "thesis"),
    ("what happened in the budget review", "budget"),
    ("who is the Thursday mentoring with", "mentor"),
]


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs, add_user_agent_edge
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.recall import RecallIndex
    from axiom.memory.trust import TrustGraph
    from axiom.rag.sqlite_store import SQLiteRAGStore
    from axiom.vega.identity.keypair import generate_keypair

    tmp: Path = tmp_path_factory.mktemp("golden")
    kp = generate_keypair()
    store = SQLiteRAGStore(f"sqlite:///{tmp}/recall.db")
    store.connect()
    svc = CompositionService(
        artifact_registry=ArtifactRegistry(
            backend=SQLiteBackend(tmp / "a.db")
        ),
        audit_log=AuditLog(tmp / "audit.jsonl", signing_keypair=kp),
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=add_user_agent_edge(AccessGraphs(), PRINCIPAL, AGENT),
        trust_graph=TrustGraph(),
        recall_index=RecallIndex(store=store, embedder=None),
    )
    ids = {}
    for key, ctype, content in _CORPUS:
        ids[key] = svc.write(
            content=content, cognitive_type=ctype,
            principal_id=PRINCIPAL, agents={AGENT}, resources=set(),
        ).id
    svc._eval_ids = ids  # test-only attachment
    return svc


def _substring_probe(service, query: str) -> list[str]:
    """The structured-filtering baseline: forget-skill style substring
    match of the whole query against fragment content JSON."""
    needle = query.lower()
    hits = []
    for art in service.artifact_registry.list(kind="fragment"):
        blob = json.dumps((art.data or {}).get("content") or {}).lower()
        if needle in blob:
            hits.append(art.name)
    return hits


class TestGoldenEvals:
    @pytest.mark.parametrize("query,target", _GOLDEN)
    def test_recall_answers_where_filtering_misses(
        self, service, query, target
    ):
        assert _substring_probe(service, query) == [], (
            "eval invalid: structured filtering answered this query — "
            "rephrase it so the pair demonstrates the gap"
        )
        result = service.recall(query, user=PRINCIPAL, agent=AGENT, k=3)
        got = [f.id for f in result.fragments]
        assert service._eval_ids[target] in got, (
            f"recall@3 miss for {query!r}: wanted {target}, got {got}"
        )

    def test_recall_at_1_majority(self, service):
        """At least 6/8 golden queries rank their target first —
        a regression tripwire on ranking quality, not a hard ceiling."""
        top1 = 0
        for query, target in _GOLDEN:
            result = service.recall(query, user=PRINCIPAL, agent=AGENT, k=1)
            if result.fragments and result.fragments[0].id == service._eval_ids[target]:
                top1 += 1
        assert top1 >= 6, f"recall@1 dropped to {top1}/8"


class TestLatencyBudget:
    def test_p95_within_working_budget(self, service):
        samples = []
        for _ in range(3):
            for query, _target in _GOLDEN:
                start = time.perf_counter()
                service.recall(query, user=PRINCIPAL, agent=AGENT, k=3)
                samples.append(time.perf_counter() - start)
        samples.sort()
        p95 = samples[int(len(samples) * 0.95) - 1]
        assert p95 < P95_BUDGET_SECONDS, (
            f"recall p95 {p95 * 1000:.1f}ms exceeds working budget "
            f"{P95_BUDGET_SECONDS * 1000:.0f}ms "
            "(docs/working/cross-mem-p1-open-questions.md)"
        )
