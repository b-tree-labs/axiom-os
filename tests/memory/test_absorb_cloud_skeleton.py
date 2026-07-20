# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for the cluster-4 absorb adapter SKELETON — cloud
account-bound memory APIs (ADR-087 D8; survey §4).

P2 knob: this cluster ships as a **credential-seamed skeleton only** —
adapter interface + fake-backed tests, **no live calls**. The seam is
``CloudMemoryClient`` (anything that lists memory records); real
HTTP clients for the round-trippable vendors (Devin Knowledge, Amp
threads, Letta Cloud) are follow-up work behind that seam. Without an
injected client the adapter degrades to a skip record naming the
credential it would need — it never constructs a transport itself.
"""

from __future__ import annotations

from pathlib import Path

PRINCIPAL = "@alice:home"
ACCOUNT = "acct-cloud"


def _make_composition(base: Path):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    base.mkdir(parents=True, exist_ok=True)
    kp = generate_keypair()
    reg = ArtifactRegistry(backend=SQLiteBackend(base / "artifacts.db"))
    audit = AuditLog(base / "audit.jsonl", signing_keypair=kp)
    return CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


class FakeClient:
    """Fake-backed CloudMemoryClient — a canned record list."""

    def __init__(self, records):
        self._records = records
        self.calls = 0

    def list_memories(self):
        self.calls += 1
        return list(self._records)


class TestCredentialSeam:
    def test_no_client_degrades_to_credential_skip(self):
        from axiom.memory.absorb.cloud_api import devin_knowledge_adapter

        adapter = devin_knowledge_adapter(account=ACCOUNT, client=None)
        scan = adapter.scan()
        assert scan.candidates == []
        assert len(scan.skipped) == 1
        assert "credentials_required" in scan.skipped[0].reason
        assert "DEVIN_API_KEY" in scan.skipped[0].reason

    def test_satisfies_adapter_protocol(self):
        from axiom.memory.absorb.base import AbsorbAdapter
        from axiom.memory.absorb.cloud_api import devin_knowledge_adapter

        adapter = devin_knowledge_adapter(account=ACCOUNT, client=None)
        assert isinstance(adapter, AbsorbAdapter)

    def test_client_error_degrades_never_crashes(self):
        from axiom.memory.absorb.cloud_api import devin_knowledge_adapter

        class Failing:
            def list_memories(self):
                raise RuntimeError("401 unauthorized")

        adapter = devin_knowledge_adapter(account=ACCOUNT, client=Failing())
        scan = adapter.scan()
        assert scan.candidates == []
        assert any("401" in s.reason for s in scan.skipped)


class TestFakeBackedVendors:
    def test_devin_knowledge_records(self):
        from axiom.memory.absorb.cloud_api import devin_knowledge_adapter

        client = FakeClient([
            {"id": "k-1", "name": "Deploy rule",
             "body": "Always deploy from tags.",
             "created_at": "2026-06-01T00:00:00+00:00"},
            {"id": "k-2", "body": "Use uv for python installs."},
            {"nonsense": True},  # junk record → skipped, not fatal
        ])
        adapter = devin_knowledge_adapter(account=ACCOUNT, client=client)
        assert adapter.harness == "devin"
        scan = adapter.scan()
        assert len(scan.candidates) == 2
        k1 = next(
            c for c in scan.candidates
            if c.origin.source_ref.endswith("k-1")
        )
        assert k1.content["text"] == "Always deploy from tags."
        assert k1.content["summary"] == "Deploy rule"
        assert k1.origin.account == ACCOUNT
        assert len(scan.skipped) == 1

    def test_amp_threads_records(self):
        from axiom.memory.absorb.cloud_api import amp_threads_adapter

        client = FakeClient([
            {"id": "T-1", "title": "Fixing the flaky test",
             "summary": "Quarantined the network-bound test."},
        ])
        adapter = amp_threads_adapter(account=ACCOUNT, client=client)
        assert adapter.harness == "amp"
        scan = adapter.scan()
        assert len(scan.candidates) == 1
        cand = scan.candidates[0]
        assert cand.content["summary"] == "Fixing the flaky test"
        assert "Quarantined" in cand.content["text"]

    def test_letta_cloud_passages(self):
        from axiom.memory.absorb.cloud_api import letta_cloud_adapter

        client = FakeClient([
            {"id": "p-1", "text": "Staging lives in us-east.",
             "created_at": "2026-06-02T00:00:00+00:00",
             "agent_id": "agent-7"},
        ])
        adapter = letta_cloud_adapter(account=ACCOUNT, client=client)
        assert adapter.harness == "letta-cloud"
        scan = adapter.scan()
        assert len(scan.candidates) == 1
        cand = scan.candidates[0]
        assert cand.content["agent_id"] == "agent-7"
        assert cand.content["event_time"] == "2026-06-02T00:00:00+00:00"


class TestFakeBackedGate:
    def test_absorb_reabsorb_noop_through_importer(self, tmp_path):
        from axiom.memory.absorb.cloud_api import devin_knowledge_adapter
        from axiom.memory.absorb.importer import import_candidates

        composition = _make_composition(tmp_path / "node")
        client = FakeClient([
            {"id": "k-1", "body": "Always deploy from tags."},
            {"id": "k-2", "body": "Use uv for python installs."},
        ])
        adapter = devin_knowledge_adapter(account=ACCOUNT, client=client)
        first = import_candidates(
            composition, adapter.scan().candidates, principal=PRINCIPAL,
        )
        assert first.imported == 2
        second = import_candidates(
            composition, adapter.scan().candidates, principal=PRINCIPAL,
        )
        assert second.imported == 0 and second.skipped_echo == 2
