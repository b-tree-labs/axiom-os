# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI tests for ``axi federation inference ls`` (ADR-030 Phase 1)."""

from __future__ import annotations

import json

from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
from axiom.extensions.builtins.federation.cli import main
from axiom.infra.gateway import LLMProvider
from axiom.memory.access import AccessGraphs
from axiom.memory.attest import AuditLog
from axiom.memory.composition import CompositionService
from axiom.memory.policy import PolicyCoord
from axiom.memory.trust import TrustGraph
from axiom.vega.federation.inference_catalog import publish_providers
from axiom.vega.identity.keypair import generate_keypair


def _seed_registry(tmp_path, advertisements):
    """Populate an artifacts.db with the given provider advertisements."""
    kp = generate_keypair()
    reg = ArtifactRegistry(backend=SQLiteBackend(tmp_path / "artifacts.db"))
    svc = CompositionService(
        artifact_registry=reg,
        audit_log=AuditLog(tmp_path / "audit.jsonl", signing_keypair=kp),
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )
    for provider, node_id in advertisements:
        publish_providers(svc, [provider], node_id=node_id, principal_id="ben")
    return tmp_path / "artifacts.db"


def _provider(name="qwen-private", tier="export_controlled", tags=()):
    return LLMProvider(
        name=name,
        endpoint="https://private.internal/v1",
        model="Qwen2.5-7B",
        uid=f"{name}-uid",
        routing_tier=tier,
        routing_tags=list(tags),
        requires_vpn=True,
    )


class TestInferenceLsCLI:
    def test_empty_when_no_registry_exists(self, tmp_path, capsys):
        rc = main(["inference", "ls", "--registry", str(tmp_path / "missing.db")])
        assert rc == 0
        out = capsys.readouterr().out
        assert "No federated inference providers advertised" in out

    def test_empty_registry_json_output(self, tmp_path, capsys):
        rc = main(
            [
                "inference",
                "ls",
                "--registry",
                str(tmp_path / "missing.db"),
                "--json",
            ]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["advertisements"] == []

    def test_lists_published_advertisements(self, tmp_path, capsys):
        db = _seed_registry(
            tmp_path,
            [
                (_provider(name="qwen-private", tier="export_controlled"), "node-a"),
                (_provider(name="gpt-4o", tier="public"), "atlas"),
            ],
        )
        rc = main(["inference", "ls", "--registry", str(db), "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        names = {a["provider_name"] for a in data}
        assert names == {"qwen-private", "gpt-4o"}

    def test_filters_by_tier(self, tmp_path, capsys):
        db = _seed_registry(
            tmp_path,
            [
                (_provider(name="qwen-private", tier="export_controlled"), "node-a"),
                (_provider(name="gpt-4o", tier="public"), "atlas"),
            ],
        )
        rc = main(
            [
                "inference",
                "ls",
                "--registry",
                str(db),
                "--tier",
                "export_controlled",
                "--json",
            ]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 1
        assert data[0]["provider_name"] == "qwen-private"

    def test_filters_by_node(self, tmp_path, capsys):
        db = _seed_registry(
            tmp_path,
            [
                (_provider(name="qwen-private"), "node-a"),
                (_provider(name="gpt-4o"), "atlas"),
            ],
        )
        rc = main(
            ["inference", "ls", "--registry", str(db), "--node", "node-a", "--json"]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 1
        assert data[0]["node_id"] == "node-a"

    def test_filters_by_tag(self, tmp_path, capsys):
        db = _seed_registry(
            tmp_path,
            [
                (_provider(name="p1", tags=("fast", "cheap")), "node-a"),
                (_provider(name="p2", tags=("cheap",)), "node-a"),
            ],
        )
        rc = main(
            ["inference", "ls", "--registry", str(db), "--tag", "fast", "--json"]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert [a["provider_name"] for a in data] == ["p1"]

    def test_json_output_shape(self, tmp_path, capsys):
        db = _seed_registry(tmp_path, [(_provider(), "node-a")])
        rc = main(["inference", "ls", "--registry", str(db), "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 1
        entry = data[0]
        required = {
            "node_id",
            "provider_name",
            "provider_uri",
            "model",
            "routing_tier",
            "routing_tags",
            "requires_vpn",
            "advertised_at",
            "fragment_id",
            "signed",
        }
        assert required.issubset(entry.keys())
        assert entry["signed"] is True

    def test_human_table_output_includes_advertised_providers(self, tmp_path, capsys):
        db = _seed_registry(
            tmp_path, [(_provider(name="qwen-private"), "node-a")]
        )
        rc = main(["inference", "ls", "--registry", str(db)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "qwen-private" in out
        assert "node-a" in out
        assert "Phase 2" in out  # footer mentions next phase

    def test_missing_inf_action_prints_usage(self, tmp_path, capsys):
        rc = main(["inference"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "usage" in err.lower()
