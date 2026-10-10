# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for CompositionService.forget() — redaction-from-recall.

`forget` is the first authorized memory mutation. It soft-deletes
(tombstones) the fragment's registry row so recall/read exclude it, while
the row + reason are retained for audit — never mutating the immutable
fragment itself. Authorization is Right.CONTROL over the fragment's
ownership (master always holds it).
"""

from __future__ import annotations


def _service(tmp_path, *, with_signing=True):
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
    return CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


def _write(svc, principal="@ben:ut", fact="x", ownership=None):
    return svc.write(
        content={"fact": fact},
        cognitive_type="semantic",
        principal_id=principal,
        agents={"axi"},
        resources={"rag"},
        ownership=ownership,
    )


class TestForgetHappyPath:
    def test_forget_removes_from_recall(self, tmp_path):
        svc = _service(tmp_path)
        frag = _write(svc)
        # present on the recall surface (artifact_registry.list — the same
        # path list_fragments_by_principal / `memory show` use) before
        assert [a.name for a in svc.artifact_registry.list(kind="fragment")] == [
            frag.id
        ]

        result = svc.forget([frag.id], requester="@ben:ut", agent="axi")

        assert result.forgotten == [frag.id]
        assert result.denied == []
        assert result.not_found == []
        # gone from recall
        assert svc.artifact_registry.list(kind="fragment") == []

    def test_forget_retains_tombstone_for_audit(self, tmp_path):
        svc = _service(tmp_path)
        frag = _write(svc)
        svc.forget([frag.id], requester="@ben:ut", agent="axi", reason="test-residue")

        tombs = svc.artifact_registry.list(kind="fragment", include_deleted=True)
        assert len(tombs) == 1
        assert tombs[0].deleted is True
        assert tombs[0].deletion_reason == "test-residue"

    def test_forget_records_audit_entry(self, tmp_path):
        svc = _service(tmp_path)
        frag = _write(svc)
        svc.forget([frag.id], requester="@ben:ut", agent="axi", reason="r")

        entries = list(svc.audit_log.query(fragment_id=frag.id, entry_type="forget"))
        assert len(entries) == 1
        assert entries[0]["principal_id"] == "@ben:ut"
        assert entries[0]["outcome"] == "ok"

    def test_forget_is_idempotent(self, tmp_path):
        svc = _service(tmp_path)
        frag = _write(svc)
        svc.forget([frag.id], requester="@ben:ut", agent="axi")
        # second forget: already gone → not_found, no crash
        result = svc.forget([frag.id], requester="@ben:ut", agent="axi")
        assert result.forgotten == []
        assert result.not_found == [frag.id]


class TestForgetAuthorization:
    def test_denies_without_control(self, tmp_path):
        svc = _service(tmp_path)
        frag = _write(svc, principal="@ben:ut")  # master = @ben:ut
        result = svc.forget([frag.id], requester="@eve:evil", agent="axi")

        assert result.denied == [frag.id]
        assert result.forgotten == []
        # fragment survives an unauthorized forget (still on the recall surface)
        assert [a.name for a in svc.artifact_registry.list(kind="fragment")] == [
            frag.id
        ]
        entries = list(
            svc.audit_log.query(fragment_id=frag.id, entry_type="forget_denied")
        )
        assert len(entries) == 1

    def test_delegated_control_can_forget(self, tmp_path):
        from axiom.memory.ownership import Right, delegate, new_ownership

        svc = _service(tmp_path)
        own = delegate(
            new_ownership(master="@ben:ut"),
            "@axi:svc",
            {Right.CONTROL},
            expires_at="2999-01-01T00:00:00+00:00",
        )
        frag = _write(svc, principal="@ben:ut", ownership=own)
        result = svc.forget([frag.id], requester="@axi:svc", agent="axi")
        assert result.forgotten == [frag.id]

    def test_delegation_without_control_denied(self, tmp_path):
        from axiom.memory.ownership import Right, delegate, new_ownership

        svc = _service(tmp_path)
        own = delegate(
            new_ownership(master="@ben:ut"),
            "@axi:svc",
            {Right.GOALS},  # not CONTROL
            expires_at="2999-01-01T00:00:00+00:00",
        )
        frag = _write(svc, principal="@ben:ut", ownership=own)
        result = svc.forget([frag.id], requester="@axi:svc", agent="axi")
        assert result.denied == [frag.id]


class TestForgetMixedBatch:
    def test_mixed_batch_partitions_results(self, tmp_path):
        svc = _service(tmp_path)
        mine = _write(svc, principal="@ben:ut", fact="a")
        theirs = _write(svc, principal="@dana:ut", fact="b")

        result = svc.forget(
            [mine.id, theirs.id, "does-not-exist"],
            requester="@ben:ut",
            agent="axi",
        )
        assert result.forgotten == [mine.id]
        assert result.denied == [theirs.id]
        assert result.not_found == ["does-not-exist"]
