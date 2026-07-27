# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Regression pins for CompositionService.read()/forget() semantics.

Written BEFORE the P1 keyed-lookup rewire (ADR-087 P1: "keyed lookups
replace scan paths on the existing read/forget surfaces") and kept
after it: observable behavior must be byte-identical. Pins the edge
semantics a naive rewrite could silently change: earliest-row wins on
duplicate names, tombstones excluded, forget tombstones every live row
carrying the id, missing/denied partitioning."""

from __future__ import annotations

from pathlib import Path

import pytest

PRINCIPAL = "@alice:pins"
AGENT = "axi"


@pytest.fixture
def service(tmp_path: Path):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import (
        AccessGraphs,
        add_agent_resource_edge,
        add_user_agent_edge,
    )
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    kp = generate_keypair()
    graphs = AccessGraphs()
    graphs = add_user_agent_edge(graphs, PRINCIPAL, AGENT)
    graphs = add_agent_resource_edge(graphs, AGENT, "r1")
    return CompositionService(
        artifact_registry=ArtifactRegistry(
            backend=SQLiteBackend(tmp_path / "a.db")
        ),
        audit_log=AuditLog(tmp_path / "audit.jsonl", signing_keypair=kp),
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=graphs,
        trust_graph=TrustGraph(),
    )


def _write(service, fact: str):
    return service.write(
        content={"fact": fact}, cognitive_type="semantic",
        principal_id=PRINCIPAL, agents={AGENT}, resources={"r1"},
    )


class TestReadPins:
    def test_read_returns_fragment_and_audits(self, service):
        frag = _write(service, "one")
        got = service.read([frag.id], user=PRINCIPAL, agent=AGENT)
        assert [f.id for f in got] == [frag.id]
        assert got[0].content == {"fact": "one"}
        reads = list(service.audit_log.query(entry_type="read"))
        assert [e["fragment_id"] for e in reads] == [frag.id]

    def test_read_missing_id_silently_skipped(self, service):
        frag = _write(service, "one")
        got = service.read(
            ["does-not-exist", frag.id], user=PRINCIPAL, agent=AGENT
        )
        assert [f.id for f in got] == [frag.id]

    def test_read_earliest_row_wins_on_duplicate_name(self, service):
        """Two registry rows under one fragment id: read() decodes the
        earliest — pinned so a keyed rewrite can't flip to latest."""
        frag = _write(service, "one")
        newer = dict(frag.to_dict())
        newer["content"] = {"fact": "shadow"}
        service.artifact_registry.register(
            kind="fragment", name=frag.id, data=newer,
        )
        got = service.read([frag.id], user=PRINCIPAL, agent=AGENT)
        assert got[0].content == {"fact": "one"}

    def test_read_excludes_tombstoned(self, service):
        frag = _write(service, "one")
        service.forget([frag.id], requester=PRINCIPAL, agent=AGENT)
        assert service.read([frag.id], user=PRINCIPAL, agent=AGENT) == []


class TestForgetPins:
    def test_forget_partitions_results(self, service):
        frag = _write(service, "one")
        result = service.forget(
            [frag.id, "missing-id"], requester=PRINCIPAL, agent=AGENT,
        )
        assert result.forgotten == [frag.id]
        assert result.not_found == ["missing-id"]
        assert result.denied == []

    def test_forget_denied_without_control(self, service):
        frag = _write(service, "one")
        result = service.forget(
            [frag.id], requester="@stranger:pins", agent=AGENT,
        )
        assert result.denied == [frag.id]
        assert service.read([frag.id], user=PRINCIPAL, agent=AGENT) != []

    def test_forget_tombstones_every_live_row(self, service):
        frag = _write(service, "one")
        dup = dict(frag.to_dict())
        service.artifact_registry.register(
            kind="fragment", name=frag.id, data=dup,
        )
        result = service.forget([frag.id], requester=PRINCIPAL, agent=AGENT)
        assert result.forgotten == [frag.id]
        rows = [
            a for a in service.artifact_registry.list(
                kind="fragment", include_deleted=True
            )
            if a.name == frag.id
        ]
        assert len(rows) == 2
        assert all(a.deleted for a in rows)

    def test_forget_is_idempotent_second_call_not_found(self, service):
        frag = _write(service, "one")
        service.forget([frag.id], requester=PRINCIPAL, agent=AGENT)
        again = service.forget([frag.id], requester=PRINCIPAL, agent=AGENT)
        assert again.not_found == [frag.id]
        assert again.forgotten == []
