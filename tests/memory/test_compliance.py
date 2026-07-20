# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Memory compliance suite — gating invariants from spec-memory.md §12.

Each test maps to a numbered invariant in spec-memory.md and / or a
benchmark in working/memory-benchmarks.md §2. Failing any of these is
a release blocker; CI runs them via ``pytest -m memory_compliance``.

Invariants checked here are deterministic; performance numbers live in
the benchmarks harness (working/memory-benchmarks-baseline.py), not
here. Some invariants (extractor classification gate, federation
gateway projection rules) become measurable only after Stages 2 + 5
land — those tests are marked as expected-pending until then so the
gate stays green during migration.
"""

from __future__ import annotations

from datetime import UTC

import pytest

from axiom.artifacts.registry import ArtifactRegistry, InMemoryBackend
from axiom.memory.access import AccessGraphs
from axiom.memory.attest import AuditLog
from axiom.memory.composition import CompositionService
from axiom.memory.policy import PolicyCoord
from axiom.memory.projections import RecentActivityProjection, TaskSpec
from axiom.memory.trust import TrustGraph
from axiom.vega.federation.policy import (
    ClassificationStamp,
    VisibilityHorizon,
)

pytestmark = pytest.mark.memory_compliance


@pytest.fixture
def cs(tmp_path):
    return CompositionService(
        artifact_registry=ArtifactRegistry(backend=InMemoryBackend()),
        audit_log=AuditLog(tmp_path / "audit.jsonl", signing_keypair=None),
        signing_keypair=None,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


def _episodic(cs, *, principal, scope, q, ts):
    return cs.write(
        content={
            "event_time": ts,
            "classroom_id": scope,
            "question": q,
            "had_answer": True,
            "citations_count": 1,
            "mode": "ask",
        },
        cognitive_type="episodic",
        principal_id=principal,
        agents=set(),
        resources=set(),
        # ADR-035: when a human acts directly, accountable == principal.
        accountable_human_id=principal,
    )


# ---------------------------------------------------------------------------
# C1 — Replay determinism (spec-memory I14)
# ---------------------------------------------------------------------------


class TestC1ReplayDeterminism:
    """Same (events, graph, task) MUST yield byte-equivalent output."""

    def test_recent_activity_projection_is_deterministic_over_n_runs(self, cs):
        for i in range(50):
            _episodic(
                cs, principal=f"s{i % 5}", scope="NE101",
                q=f"Q{i}", ts=f"2026-04-26T1{i % 10}:{(i // 10) % 60:02d}:00+00:00",
            )

        proj = RecentActivityProjection(cs.artifact_registry, window_n=5)
        task = TaskSpec(task_type="recent_activity", scope="NE101")

        canonical = None
        for _ in range(20):
            result = proj.project(task, principal_id="s0")
            ids = tuple(f.id for f in result.fragments)
            if canonical is None:
                canonical = ids
            assert ids == canonical, "projection drift detected"


# ---------------------------------------------------------------------------
# C3 — Per-scope isolation (spec-memory I1)
# ---------------------------------------------------------------------------


class TestC3PerScopeIsolation:
    """RecentActivityProjection MUST never return scope-B fragments
    when asked for scope A."""

    def test_projection_filters_by_scope(self, cs):
        _episodic(cs, principal="alice", scope="NE101",
                  q="ne101 q", ts="2026-04-26T10:00:00+00:00")
        _episodic(cs, principal="alice", scope="NE102",
                  q="ne102 q", ts="2026-04-26T11:00:00+00:00")

        proj = RecentActivityProjection(cs.artifact_registry)
        ne101 = proj.project(
            TaskSpec(task_type="recent_activity", scope="NE101"),
            principal_id="alice",
        )
        ne102 = proj.project(
            TaskSpec(task_type="recent_activity", scope="NE102"),
            principal_id="alice",
        )
        assert [f.content["question"] for f in ne101.fragments] == ["ne101 q"]
        assert [f.content["question"] for f in ne102.fragments] == ["ne102 q"]


# ---------------------------------------------------------------------------
# C5 — Classification trumps visibility (spec-federation-policy §4)
# ---------------------------------------------------------------------------


class TestC5ClassificationTrumpsVisibility:
    """A CUI fragment with PUBLIC visibility MUST collapse to
    SCOPE_INTERNAL outflow at the gateway. Composition rule:
    effective = min(visibility, classification.allowed_outflow_level())."""

    def test_cui_collapses_optimistic_public(self):
        cui_stamp = ClassificationStamp(level="cui")
        effective = VisibilityHorizon.most_restrictive(
            VisibilityHorizon.PUBLIC,
            cui_stamp.allowed_outflow_level(),
        )
        assert effective is VisibilityHorizon.PEERS_DECLARED

    def test_secret_collapses_to_scope_internal(self):
        secret_stamp = ClassificationStamp(level="secret")
        effective = VisibilityHorizon.most_restrictive(
            VisibilityHorizon.PUBLIC,
            secret_stamp.allowed_outflow_level(),
        )
        assert effective is VisibilityHorizon.SCOPE_INTERNAL

    def test_compartmented_unclassified_still_collapses(self):
        """Even unclassified-but-compartmented content stays in scope."""
        stamp = ClassificationStamp(
            level="unclassified",
            compartments=frozenset({"NOFORN"}),
        )
        effective = VisibilityHorizon.most_restrictive(
            VisibilityHorizon.PUBLIC,
            stamp.allowed_outflow_level(),
        )
        assert effective is VisibilityHorizon.SCOPE_INTERNAL


# ---------------------------------------------------------------------------
# C6 — Provenance integrity (spec-memory I10, I16)
# ---------------------------------------------------------------------------


class TestC6ProvenanceIntegrity:
    """Every projection MUST cite the fragments it composed."""

    def test_recent_activity_carries_composing_fragments(self, cs):
        ids = []
        for i in range(3):
            f = _episodic(
                cs, principal="alice", scope="NE101",
                q=f"Q{i}", ts=f"2026-04-26T1{i}:00:00+00:00",
            )
            ids.append(f.id)

        proj = RecentActivityProjection(cs.artifact_registry, window_n=3)
        result = proj.project(
            TaskSpec(task_type="recent_activity", scope="NE101"),
            principal_id="alice",
        )
        # Every returned fragment carries the (T, U, A, R) provenance and
        # an id that can be traced back to L1.
        returned_ids = {f.id for f in result.fragments}
        assert returned_ids == set(ids)
        for f in result.fragments:
            assert f.provenance.principal_id == "alice"
            assert f.provenance.timestamp


# ---------------------------------------------------------------------------
# C8 — Forget actually erases (spec-memory I4, I15)
# ---------------------------------------------------------------------------


class TestC8ForgetActuallyErases:
    """After tombstoning, the fragment MUST NOT appear in any
    projection's output. Tested today against the classroom interaction
    store's tombstone primitive (the scope of forget at Stage 1); the
    L1 tombstone primitive applies the same contract once promoted."""

    def test_classroom_forget_propagates_through_summary(self, tmp_path):
        from axiom.extensions.builtins.classroom.classroom_interaction import (
            ClassroomInteractionStore,
            InteractionRecord,
        )

        store = ClassroomInteractionStore(tmp_path)
        for i in range(3):
            store.append(InteractionRecord(
                student_id="alice", question=f"Q{i}", had_answer=True,
                citations_count=1, timestamp=f"2026-04-26T1{i}:00:00+00:00",
                classroom_id="NE101", mode="ask",
            ))
        target = store.list()[0]
        store.forget(student_id="alice", interaction_id=target.interaction_id)

        # Tombstoned fragment MUST be absent from every projection.
        listed = store.list()
        assert all(f.interaction_id != target.interaction_id for f in listed)
        summary = store.summary_for_student("alice")
        questions = [r["question"] for r in summary["recent_questions"]]
        assert target.question not in questions
        # Audit trail preserved on disk per spec-memory I2 (append-only).
        raw = (tmp_path / "interactions.jsonl").read_text().splitlines()
        assert len(raw) == 4   # 3 originals + 1 tombstone


# ---------------------------------------------------------------------------
# C10 — Backward-compat decode (spec-memory §3.1)
# ---------------------------------------------------------------------------


class TestC10BackwardCompatDecode:
    """Legacy fragment dicts (no visibility, no classification, etc.)
    MUST decode with documented defaults rather than KeyError."""

    def test_decode_without_visibility_uses_default(self):
        from axiom.memory.fragment import create_fragment, fragment_from_dict

        frag = create_fragment(
            content={"x": 1}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        d = frag.to_dict()
        del d["visibility"]
        decoded = fragment_from_dict(d)
        assert decoded.visibility is VisibilityHorizon.SCOPE_INTERNAL

    def test_decode_without_classification_uses_default(self):
        from axiom.memory.fragment import create_fragment, fragment_from_dict

        frag = create_fragment(
            content={"x": 1}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        d = frag.to_dict()
        del d["classification"]
        decoded = fragment_from_dict(d)
        assert decoded.classification == ClassificationStamp.unclassified()


# ---------------------------------------------------------------------------
# Round-trip integrity — the spec's serialization contract
# ---------------------------------------------------------------------------


class TestRoundTripIntegrity:
    """Per spec-memory §3.1: to_dict/from_dict MUST be a stable round trip."""

    def test_full_fragment_round_trip(self, cs):
        import dataclasses

        from axiom.memory.fragment import fragment_from_dict
        from axiom.vega.federation.policy import (
            ExportControl,
            ProprietaryRestriction,
        )

        f = cs.write(
            content={"event_time": "2026-04-26T10:00:00+00:00",
                     "question": "Q", "mode": "ask",
                     "had_answer": True, "citations_count": 1,
                     "classroom_id": "NE101"},
            cognitive_type="episodic", principal_id="u1",
            agents=set(), resources=set(),
            accountable_human_id="u1",
        )
        # Decorate with non-default visibility + classification.
        decorated = dataclasses.replace(
            f,
            visibility=VisibilityHorizon.PEERS_DECLARED,
            classification=ClassificationStamp(
                level="cui",
                export_control=ExportControl(itar=True),
                proprietary=ProprietaryRestriction(restricted=True, license="NDA"),
                original_classifier="@officer:doe",
                classification_date="2026-04-26T00:00:00+00:00",
            ),
        )
        decoded = fragment_from_dict(decorated.to_dict())
        assert decoded.visibility is VisibilityHorizon.PEERS_DECLARED
        assert decoded.classification == decorated.classification
        assert decoded.id == decorated.id
        assert decoded.cognitive_type == decorated.cognitive_type


# ---------------------------------------------------------------------------
# Pending — invariants that become measurable once later stages land.
# ---------------------------------------------------------------------------


class TestC7ExtractorClassificationGate:
    """Stage 2 lands the extractor registry. C7 (spec-memory I11):
    a CUI fragment MUST never receive an extractor whose
    capability.max_classification < cui. Implementation: the registry
    skips capability-mismatched extractors and surfaces them in
    ``extractors_skipped_classification``."""

    def test_external_extractor_rejected_for_cui(self, tmp_path):
        import dataclasses

        from axiom.memory.fragment import create_fragment
        from axiom.memory.graph import (
            ExtractorCapability,
            ExtractorRegistry,
            SQLiteConceptGraph,
        )
        from axiom.vega.federation.policy import ClassificationStamp

        invocations = []

        class _ExternalExtractor:
            capability = ExtractorCapability(
                name="external_unclass_only",
                runs_on="external_provider",
                provider_id="some_cloud_llm",
                logs_to=("provider_metrics",),
                max_classification="unclassified",
            )

            @property
            def handles(self):
                from axiom.memory.fragment import CognitiveType
                return frozenset({CognitiveType.EPISODIC})

            def extract(self, fragment):
                invocations.append(fragment.id)
                return []

            def link(self, fragment, existing):
                return []

        graph = SQLiteConceptGraph(tmp_path / "g.db")
        registry = ExtractorRegistry(graph=graph)
        registry.register(_ExternalExtractor())

        cui_frag = dataclasses.replace(
            create_fragment(
                content={
                    "event_time": "2026-04-26T10:00:00+00:00",
                    "question": "anything", "had_answer": True,
                    "citations_count": 1,
                },
                cognitive_type="episodic",
                principal_id="alice", agents=set(), resources=set(),
            ),
            classification=ClassificationStamp(level="cui"),
        )
        result = registry.run_for_fragment(cui_frag)

        # Hard requirement: external extractor was NOT invoked.
        assert invocations == []
        assert "external_unclass_only" in result["extractors_skipped_classification"]


class TestC6ConceptsCiteExtractedFrom:
    """Stage 2 spec-memory I10: every Concept MUST carry a non-empty
    ``extracted_from`` list pointing back to the L1 fragment(s) that
    produced it. No orphan concepts."""

    def test_deterministic_extractor_concepts_cite_source(self):
        from axiom.memory.fragment import create_fragment
        from axiom.memory.graph import DeterministicTextExtractor

        frag = create_fragment(
            content={
                "event_time": "2026-04-26T10:00:00+00:00",
                "question": "criticality",
                "had_answer": True, "citations_count": 1,
            },
            cognitive_type="episodic",
            principal_id="alice", agents=set(), resources=set(),
        )
        concepts = DeterministicTextExtractor().extract(frag)
        assert concepts, "expected at least one concept extracted"
        for c in concepts:
            assert frag.id in c.extracted_from


class TestC4FederationGatewayVisibility:
    """Stage 5a (compliance C4): the FederationGateway MUST enforce
    VisibilityHorizon at outbound projection time and at inbound
    acceptance time. Stage 5b (post-Prague) adds classification +
    nationality enforcement on top of the same gates.

    Per ``docs/specs/spec-federation-policy.md §6`` and
    ``docs/working/memory-roadmap.md`` Stage 5a.
    """

    @staticmethod
    def _gateway(*, declared=frozenset(),
                 inbound_horizons=frozenset({VisibilityHorizon.PEERS_DECLARED})):
        from axiom.vega.federation.gateway import FederationGateway
        from axiom.vega.federation.policy import TrustProfile

        return FederationGateway(
            scope_id="ne101-prague",
            trust_profile=TrustProfile(
                scope="ne101-prague",
                declared_peers=declared,
                inbound_horizons=inbound_horizons,
            ),
            signer=lambda b: "sig",
            verifier=lambda b, s, p: True,
        )

    @staticmethod
    def _frag(visibility):
        import dataclasses

        from axiom.memory.fragment import create_fragment

        base = create_fragment(
            content={"text": "x"}, cognitive_type="semantic",
            principal_id="alice", agents=set(), resources=set(),
        )
        return dataclasses.replace(base, visibility=visibility)

    def test_c4_outbound_scope_internal_never_leaves(self):
        """SCOPE_INTERNAL fragments MUST be skipped at outbound projection
        regardless of peer status."""
        gw = self._gateway(declared=frozenset({"@partner-1"}))
        f = self._frag(VisibilityHorizon.SCOPE_INTERNAL)
        signed = gw.project_for_peer(
            projection={"fragments": [f.to_dict()]},
            peer_id="@partner-1",
        )
        assert signed.payload["fragments"] == []

    def test_c4_outbound_default_deny_to_undeclared_peer(self):
        """An undeclared peer MUST receive nothing below PUBLIC."""
        gw = self._gateway(declared=frozenset())
        peers_declared = self._frag(VisibilityHorizon.PEERS_DECLARED)
        federation_bound = self._frag(VisibilityHorizon.FEDERATION_BOUND)
        signed = gw.project_for_peer(
            projection={"fragments": [
                peers_declared.to_dict(), federation_bound.to_dict(),
            ]},
            peer_id="@stranger",
        )
        assert signed.payload["fragments"] == []

    def test_c4_inbound_horizon_filter(self):
        """An inbound projection's individual fragments MUST be filtered to
        only those whose horizon is in trust_profile.inbound_horizons."""
        from datetime import datetime

        from axiom.vega.federation.gateway import SignedProjection

        gw = self._gateway(
            declared=frozenset({"@partner-1"}),
            inbound_horizons=frozenset({VisibilityHorizon.PEERS_DECLARED}),
        )
        ok = self._frag(VisibilityHorizon.PEERS_DECLARED)
        rejected = self._frag(VisibilityHorizon.PUBLIC)
        incoming = SignedProjection(
            payload={"fragments": [ok.to_dict(), rejected.to_dict()]},
            origin_scope="@partner-1",
            target_peer="ne101-prague",
            signature="sig",
            signed_at=datetime.now(UTC),
            horizon_max=VisibilityHorizon.PUBLIC,
        )
        decision = gw.accept_from_peer(incoming, peer_id="@partner-1")
        assert decision.fragments_accepted == 1
        assert decision.fragments_rejected == 1

    def test_c4_default_visibility_is_scope_internal_and_never_leaves(self):
        """A fragment created without explicit visibility defaults to
        SCOPE_INTERNAL and MUST never leave."""
        from axiom.memory.fragment import create_fragment

        # No dataclasses.replace — confirm the *default* is default-deny.
        f = create_fragment(
            content={"text": "x"}, cognitive_type="semantic",
            principal_id="alice", agents=set(), resources=set(),
        )
        assert f.visibility is VisibilityHorizon.SCOPE_INTERNAL

        gw = self._gateway(declared=frozenset({"@partner-1"}))
        signed = gw.project_for_peer(
            projection={"fragments": [f.to_dict()]},
            peer_id="@partner-1",
        )
        assert signed.payload["fragments"] == []


class TestC9PendingHopBoundFederation:
    """Stage 5b: full hop-bounded trust-graph traversal lands post-Prague."""

    def test_c9_max_hops_above_one_blocked_at_gateway(self):
        """Stage 5a guards the door: max_hops > 1 raises until Stage 5b
        wires the trust graph."""
        from axiom.vega.federation.gateway import FederationGateway
        from axiom.vega.federation.policy import TrustProfile

        gw = FederationGateway(
            scope_id="ne101-prague",
            trust_profile=TrustProfile(scope="ne101-prague"),
            signer=lambda b: "sig",
            verifier=lambda b, s, p: True,
        )
        with pytest.raises(NotImplementedError, match="Stage 5b"):
            gw.project_for_peer(
                projection={"fragments": []}, peer_id="@p", max_hops=2,
            )


# ---------------------------------------------------------------------------
# Accountability compliance — ADR-035 release gate
# ---------------------------------------------------------------------------


@pytest.mark.accountability_compliance
class TestAccountabilityCompliance:
    """ADR-035 §D7 + 'Compliance gates introduced'. These pin the
    accountable-human binding contract as a release gate alongside
    memory_compliance.
    """

    def test_every_current_fragment_has_accountable_human(self, cs):
        """Sweeps a small fixture cohort. Every current-version fragment
        MUST carry a non-empty accountable_human_id."""
        from axiom.memory.fragment import (
            CURRENT_SCHEMA_VERSION,
            fragment_from_dict,
        )

        # Write a few fragments through the CompositionService.
        for i, principal in enumerate(["@ben:example-org", "@alice:example-org", "@max:example-org"]):
            cs.write(
                content={
                    "event_time": f"2026-04-26T1{i}:00:00+00:00",
                    "question": f"q{i}",
                    "had_answer": True,
                    "citations_count": 1,
                    "classroom_id": "NE101",
                    "mode": "ask",
                },
                cognitive_type="episodic",
                principal_id=principal,
                agents=set(),
                resources=set(),
                accountable_human_id=principal,
            )
        for artifact in cs.artifact_registry.list(kind="fragment"):
            frag = fragment_from_dict(artifact.data)
            assert frag.schema_version == CURRENT_SCHEMA_VERSION
            ah = frag.provenance.accountable_human_id
            assert ah and not ah.startswith("legacy:"), (
                f"fragment {frag.id} has invalid accountable_human_id={ah!r}"
            )

    def test_v1_fragments_decode_under_v2_axiom(self):
        """A pinned v1 payload (pre-ADR-035) MUST decode cleanly under
        the current decoder, with the legacy sentinel filled in."""
        from axiom.memory.fragment import fragment_from_dict

        v1_payload = {
            "id": "frozen-v1",
            "cognitive_type": "semantic",
            "content": {"fact": "frozen v1"},
            "provenance": {
                "timestamp": "2025-12-31T23:59:59+00:00",
                "principal_id": "@anyone:demo",
                "agents": [],
                "resources": [],
            },
            "retention_tier": "active",
            "visibility": "scope_internal",
            "classification": {"level": "unclassified"},
        }
        decoded = fragment_from_dict(v1_payload)
        assert decoded.provenance.accountable_human_id == "legacy:unattributed"
        assert decoded.provenance.delegation_chain == ()

    def test_compositionservice_rejects_unset_accountable_human(self, cs):
        """Failure path: write with empty accountable_human_id raises
        AccountabilityError before any persistence."""
        from axiom.memory.exceptions import AccountabilityError

        with pytest.raises(AccountabilityError):
            cs.write(
                content={"x": 1},
                cognitive_type="semantic",
                principal_id="@ben:example-org",
                agents=set(),
                resources=set(),
                accountable_human_id="",
            )
