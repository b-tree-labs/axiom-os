# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for CompositionService (#70).

Single entry point through which every memory operation passes.
Proves the memory primitives compose — every call exercises
policy + access + gating + attestation + persistence + audit.

The CompositionService is generic (core). Classroom bootstrapping
layer is tested separately (tests/classroom_unit/test_composition.py).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Helpers — build a fully-wired service
# ---------------------------------------------------------------------------


def _service(tmp_path, *, with_signing=True, with_transform=False):
    """Construct a CompositionService with every primitive wired."""
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    kp = generate_keypair() if with_signing else None
    reg = ArtifactRegistry(backend=SQLiteBackend(tmp_path / "artifacts.db"))
    audit = AuditLog(tmp_path / "audit.jsonl", signing_keypair=kp)
    policy = PolicyCoord(global_policy={"write": "private"})
    access = AccessGraphs()
    trust = TrustGraph()

    transform = None
    if with_transform:
        from axiom.memory.write_policy import anonymize_principal

        transform = anonymize_principal

    return CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=kp,
        policy_coord=policy,
        access_graphs=access,
        trust_graph=trust,
        transform=transform,
    )


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


class TestWrite:
    def test_write_returns_fragment_with_id(self, tmp_path):
        svc = _service(tmp_path)
        frag = svc.write(
            content={"fact": "x"},
            cognitive_type="semantic",
            principal_id="@ben:ut",
            agents={"axi"},
            resources={"rag-org"},
        )
        assert frag.id
        assert frag.cognitive_type.value == "semantic"

    def test_write_signs_when_keypair_configured(self, tmp_path):
        from axiom.memory.attest import verify_fragment_signature

        svc = _service(tmp_path, with_signing=True)
        frag = svc.write(
            content={"fact": "x"},
            cognitive_type="semantic",
            principal_id="@ben:ut",
            agents=set(),
            resources=set(),
        )
        assert frag.signature is not None
        assert verify_fragment_signature(
            frag, svc.signing_keypair.public_bytes
        ) is True

    def test_write_skips_signing_without_keypair(self, tmp_path):
        svc = _service(tmp_path, with_signing=False)
        frag = svc.write(
            content={"fact": "x"}, cognitive_type="semantic",
            principal_id="@ben:ut", agents=set(), resources=set(),
        )
        assert frag.signature is None

    def test_write_attaches_default_ownership(self, tmp_path):
        svc = _service(tmp_path)
        frag = svc.write(
            content={"fact": "x"}, cognitive_type="semantic",
            principal_id="@ben:ut", agents=set(), resources=set(),
        )
        assert frag.ownership is not None
        assert frag.ownership.master == "@ben:ut"

    def test_write_respects_provided_ownership(self, tmp_path):
        from axiom.memory.ownership import new_ownership

        svc = _service(tmp_path)
        own = new_ownership(master="@alice:ut")
        frag = svc.write(
            content={"fact": "x"}, cognitive_type="semantic",
            principal_id="@ben:ut", agents=set(), resources=set(),
            ownership=own,
        )
        assert frag.ownership.master == "@alice:ut"

    def test_write_persists_to_registry(self, tmp_path):
        svc = _service(tmp_path)
        frag = svc.write(
            content={"fact": "persisted"}, cognitive_type="semantic",
            principal_id="@ben:ut", agents=set(), resources=set(),
        )
        # Verify: reopen the registry and find the fragment
        from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend

        reg2 = ArtifactRegistry(backend=SQLiteBackend(tmp_path / "artifacts.db"))
        listed = reg2.list(kind="fragment")
        assert len(listed) == 1
        assert listed[0].data["id"] == frag.id

    def test_write_records_audit_entry(self, tmp_path):
        svc = _service(tmp_path)
        frag = svc.write(
            content={"fact": "x"}, cognitive_type="semantic",
            principal_id="@ben:ut", agents=set(), resources=set(),
        )
        entries = list(svc.audit_log.read_all())
        assert len(entries) == 1
        assert entries[0]["entry_type"] == "write"
        assert entries[0]["fragment_id"] == frag.id

    def test_write_shared_scope_runs_transform(self, tmp_path):
        from axiom.memory.policy import with_global

        svc = _service(tmp_path, with_transform=True)
        svc.policy_coord = with_global(svc.policy_coord, {"write": "shared"})
        frag = svc.write(
            content={"fact": "x"}, cognitive_type="semantic",
            principal_id="@ben:ut", agents=set(), resources=set(),
        )
        # anonymize_principal transform applied — principal becomes pseudonym
        assert frag.provenance.principal_id.startswith("anon-")

    def test_write_private_scope_skips_transform(self, tmp_path):
        svc = _service(tmp_path, with_transform=True)
        # Default global_policy is {"write": "private"}
        frag = svc.write(
            content={"fact": "x"}, cognitive_type="semantic",
            principal_id="@ben:ut", agents=set(), resources=set(),
        )
        # Principal stays as-is in private scope
        assert frag.provenance.principal_id == "@ben:ut"


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------


class TestRead:
    def _write_sample(self, svc):
        return svc.write(
            content={"fact": "sample"}, cognitive_type="semantic",
            principal_id="@ben:ut",
            agents={"axi"},
            resources={"rag-org"},
        )

    def test_owner_reads_own_without_access_edges(self, tmp_path):
        """ADR-026 (OQ-A2-1 fix): the master reads their OWN fragment through
        read() even with an empty access graph. This is the intrinsic owner
        read right — not graph-derived — that makes self-recall work."""
        svc = _service(tmp_path)
        frag = self._write_sample(svc)  # ownership master defaults to @ben:ut
        results = svc.read(
            fragment_ids=[frag.id], user="@ben:ut", agent="axi"
        )
        assert [f.id for f in results] == [frag.id]

    def test_read_filters_by_access(self, tmp_path):
        from axiom.memory.access import (
            add_agent_resource_edge,
            add_user_agent_edge,
        )

        svc = _service(tmp_path)
        frag = self._write_sample(svc)  # owned by @ben:ut
        # A NON-owner's visibility is purely graph-derived. No access edges →
        # the peer cannot see @ben's fragment (the ownership base case only
        # helps the owner, so no cross-principal path is opened).
        peer = "@peer:ut"
        results = svc.read(fragment_ids=[frag.id], user=peer, agent="axi")
        assert results == []

        # Grant the peer graph access → now visible
        svc.access_graphs = add_user_agent_edge(svc.access_graphs, peer, "axi")
        svc.access_graphs = add_agent_resource_edge(
            svc.access_graphs, "axi", "rag-org"
        )
        results = svc.read(fragment_ids=[frag.id], user=peer, agent="axi")
        assert len(results) == 1
        assert results[0].id == frag.id

    def test_read_records_audit_entries(self, tmp_path):
        from axiom.memory.access import (
            add_agent_resource_edge,
            add_user_agent_edge,
        )

        svc = _service(tmp_path)
        frag = self._write_sample(svc)
        svc.access_graphs = add_user_agent_edge(
            svc.access_graphs, "@ben:ut", "axi"
        )
        svc.access_graphs = add_agent_resource_edge(
            svc.access_graphs, "axi", "rag-org"
        )

        svc.read(fragment_ids=[frag.id], user="@ben:ut", agent="axi")

        entries = list(svc.audit_log.read_all())
        read_entries = [e for e in entries if e["entry_type"] == "read"]
        assert len(read_entries) == 1
        assert read_entries[0]["fragment_id"] == frag.id

    def test_read_denied_fragment_recorded_as_denial(self, tmp_path):
        svc = _service(tmp_path)
        frag = self._write_sample(svc)  # owned by @ben:ut
        # A NON-owner with no access edges is denied — and the denial is
        # audited so revocation-time-of-flight stays reconstructible.
        svc.read(fragment_ids=[frag.id], user="@peer:ut", agent="axi")

        entries = list(svc.audit_log.read_all())
        denial = [e for e in entries if e["entry_type"] == "read_denied"]
        assert len(denial) == 1
        assert denial[0]["fragment_id"] == frag.id

    def test_read_missing_id_skipped_gracefully(self, tmp_path):
        svc = _service(tmp_path)
        results = svc.read(
            fragment_ids=["does-not-exist"], user="@ben:ut", agent="axi",
        )
        assert results == []


# ---------------------------------------------------------------------------
# LLM response path
# ---------------------------------------------------------------------------


class TestLLMResponse:
    def test_clean_output_passes_through(self, tmp_path):
        svc = _service(tmp_path)
        result = svc.llm_response(
            output="Regular helpful response.",
            user="@ben:ut",
            agent="axi",
            visible_fragments=[],
            all_fragments=[],
        )
        assert result.is_clean is True

    def test_breach_detected(self, tmp_path):
        import dataclasses

        from axiom.memory.fragment import create_fragment

        svc = _service(tmp_path)

        # A fragment the user doesn't have access to
        secret_base = create_fragment(
            content={"fact": "the password is hunter2 and it is secret"},
            cognitive_type="semantic",
            principal_id="@other:x", agents=set(), resources=set(),
        )
        # Craft an id that matches the UUID regex so post_filter can detect
        secret = dataclasses.replace(
            secret_base, id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        )

        # LLM quoted secret content verbatim
        result = svc.llm_response(
            output="The password is hunter2 and it is secret according to my notes.",
            user="@ben:ut",
            agent="axi",
            visible_fragments=[],
            all_fragments=[secret],
            min_quote_words=5,
        )
        assert result.is_clean is False
        assert len(result.breaches) >= 1


# ---------------------------------------------------------------------------
# Bootstrap helper (classroom-layer)
# ---------------------------------------------------------------------------


class TestClassroomComposition:
    def test_bootstrap_produces_fully_wired_service(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        from axiom.extensions.builtins.classroom.composition_boot import (
            build_classroom_composition,
        )

        svc = build_classroom_composition(classroom_id="prague-s26")
        assert svc.signing_keypair is not None
        assert svc.audit_log is not None
        assert svc.artifact_registry is not None
        # Can immediately write
        frag = svc.write(
            content={"fact": "bootstrap check"},
            cognitive_type="semantic",
            principal_id="@ben:ut",
            agents=set(),
            resources=set(),
        )
        assert frag.id

    def test_bootstrap_persists_state_per_classroom(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        from axiom.extensions.builtins.classroom.composition_boot import (
            build_classroom_composition,
        )

        svc1 = build_classroom_composition(classroom_id="cr-a")
        frag = svc1.write(
            content={"fact": "x"}, cognitive_type="semantic",
            principal_id="@ben:ut", agents=set(), resources=set(),
        )

        svc2 = build_classroom_composition(classroom_id="cr-a")
        # Fresh service, same classroom — artifact registry survives
        listed = svc2.artifact_registry.list(kind="fragment")
        assert len(listed) == 1
        assert listed[0].data["id"] == frag.id


# ---------------------------------------------------------------------------
# Optional primitives
# ---------------------------------------------------------------------------


class TestOptionalPrimitives:
    def test_service_works_without_signing_keypair(self, tmp_path):
        svc = _service(tmp_path, with_signing=False)
        frag = svc.write(
            content={"fact": "x"}, cognitive_type="semantic",
            principal_id="@ben:ut", agents=set(), resources=set(),
        )
        assert frag.id
        assert frag.signature is None

    def test_service_reads_unsigned_fragments(self, tmp_path):
        from axiom.memory.access import (
            add_agent_resource_edge,
            add_user_agent_edge,
        )

        svc = _service(tmp_path, with_signing=False)
        frag = svc.write(
            content={"fact": "x"}, cognitive_type="semantic",
            principal_id="@ben:ut",
            agents={"axi"}, resources={"rag-org"},
        )
        svc.access_graphs = add_user_agent_edge(
            svc.access_graphs, "@ben:ut", "axi"
        )
        svc.access_graphs = add_agent_resource_edge(
            svc.access_graphs, "axi", "rag-org"
        )
        results = svc.read(
            fragment_ids=[frag.id], user="@ben:ut", agent="axi"
        )
        assert len(results) == 1
