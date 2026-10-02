# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-030 Phase 1: federated inference capability catalog.

Phase 1 is read-only advertisement — nodes publish `federated_provider`
MemoryFragments describing each exposed LLM provider; consumers
enumerate them. No routing, no policy enforcement. Those are Phase 2+.

These tests prove:
  - Advertisement content has the shape the router will rely on later.
  - Publish writes one signed fragment per local provider through the
    CompositionService (ADR-029 four-primitives conformance).
  - Enumeration round-trips correctly and supports tier/tag filters.
  - Stale advertisements (past their TTL cutoff) are excluded.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
from axiom.infra.gateway import LLMProvider
from axiom.memory.access import AccessGraphs
from axiom.memory.attest import AuditLog
from axiom.memory.composition import CompositionService
from axiom.memory.policy import PolicyCoord
from axiom.memory.trust import TrustGraph
from axiom.vega.federation.inference_catalog import (
    FACT_KIND,
    ProviderAdvertisement,
    build_advertisement_content,
    list_advertisements,
    publish_providers,
)
from axiom.vega.identity.keypair import generate_keypair


def _service(tmp_path):
    kp = generate_keypair()
    reg = ArtifactRegistry(backend=SQLiteBackend(tmp_path / "artifacts.db"))
    audit = AuditLog(tmp_path / "audit.jsonl", signing_keypair=kp)
    policy = PolicyCoord(global_policy={"write": "private"})
    return CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=kp,
        policy_coord=policy,
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


def _provider(name="qwen-hpc", model="Qwen2.5-7B", tier="private_network", tags=()):
    return LLMProvider(
        name=name,
        endpoint="https://qwen.private.internal/v1",
        model=model,
        uid=f"{name}-uid",
        routing_tier=tier,
        routing_tags=list(tags),
        requires_vpn=True,
    )


class TestBuildAdvertisementContent:
    def test_includes_fact_kind_marker(self):
        content = build_advertisement_content(_provider(), node_id="example-host")
        assert content["fact_kind"] == FACT_KIND

    def test_builds_axiom_uri_from_node_and_provider(self):
        content = build_advertisement_content(
            _provider(name="qwen-hpc"), node_id="example-host"
        )
        assert content["provider_uri"] == "axiom://example-host/inference/qwen-hpc"

    def test_carries_capability_fields_router_needs(self):
        content = build_advertisement_content(
            _provider(
                name="qwen-hpc",
                model="Qwen2.5-7B",
                tier="export_controlled",
                tags=("private_network", "ec_whitelist"),
            ),
            node_id="example-host",
        )
        assert content["node_id"] == "example-host"
        assert content["provider_name"] == "qwen-hpc"
        assert content["model"] == "Qwen2.5-7B"
        assert content["routing_tier"] == "export_controlled"
        assert content["routing_tags"] == ["private_network", "ec_whitelist"]
        assert content["requires_vpn"] is True

    def test_advertised_at_is_iso8601(self):
        content = build_advertisement_content(_provider(), node_id="example-host")
        # Must parse as ISO-8601
        parsed = datetime.fromisoformat(content["advertised_at"])
        assert parsed.tzinfo is not None, "advertised_at must be timezone-aware"


class TestPublishProviders:
    def test_writes_one_fragment_per_provider(self, tmp_path):
        svc = _service(tmp_path)
        providers = [_provider(name="p1"), _provider(name="p2"), _provider(name="p3")]
        ids = publish_providers(svc, providers, node_id="example-host", principal_id="ben")
        assert len(ids) == 3
        assert len(set(ids)) == 3  # distinct fragment ids

    def test_written_fragments_are_signed(self, tmp_path):
        svc = _service(tmp_path)
        publish_providers(svc, [_provider()], node_id="example-host", principal_id="ben")
        artifacts = svc.artifact_registry.list(kind="fragment")
        signed = [a for a in artifacts if a.data.get("signature")]
        assert len(signed) == 1, "publish must route through CompositionService signing"

    def test_written_fragments_carry_principal_id(self, tmp_path):
        svc = _service(tmp_path)
        publish_providers(svc, [_provider()], node_id="example-host", principal_id="ben")
        artifacts = svc.artifact_registry.list(kind="fragment")
        assert artifacts[0].data["provenance"]["principal_id"] == "ben"


class TestListAdvertisements:
    def test_enumerates_all_published(self, tmp_path):
        svc = _service(tmp_path)
        publish_providers(
            svc,
            [_provider(name="p1"), _provider(name="p2")],
            node_id="example-host",
            principal_id="ben",
        )

        ads = list_advertisements(svc.artifact_registry)
        assert len(ads) == 2
        assert {a.provider_name for a in ads} == {"p1", "p2"}

    def test_returns_provideradvertisement_instances(self, tmp_path):
        svc = _service(tmp_path)
        publish_providers(svc, [_provider()], node_id="example-host", principal_id="ben")
        ads = list_advertisements(svc.artifact_registry)
        assert isinstance(ads[0], ProviderAdvertisement)

    def test_ignores_non_federated_provider_fragments(self, tmp_path):
        svc = _service(tmp_path)
        svc.write(
            content={
                "fact_kind": "something_else",
                "event_time": datetime.now(UTC).isoformat(),
                "data": "x",
            },
            cognitive_type="episodic",
            principal_id="ben",
            agents=set(),
            resources=set(),
        )
        publish_providers(svc, [_provider()], node_id="example-host", principal_id="ben")
        ads = list_advertisements(svc.artifact_registry)
        assert len(ads) == 1
        assert ads[0].provider_name == "qwen-hpc"

    def test_filters_by_tier(self, tmp_path):
        svc = _service(tmp_path)
        publish_providers(
            svc,
            [
                _provider(name="ec-1", tier="export_controlled"),
                _provider(name="pub-1", tier="public"),
                _provider(name="ec-2", tier="export_controlled"),
            ],
            node_id="example-host",
            principal_id="ben",
        )
        ads = list_advertisements(svc.artifact_registry, tier="export_controlled")
        assert {a.provider_name for a in ads} == {"ec-1", "ec-2"}

    def test_filters_by_tag(self, tmp_path):
        svc = _service(tmp_path)
        publish_providers(
            svc,
            [
                _provider(name="p1", tags=("fast", "cheap")),
                _provider(name="p2", tags=("cheap",)),
                _provider(name="p3", tags=("fast",)),
            ],
            node_id="example-host",
            principal_id="ben",
        )
        ads = list_advertisements(svc.artifact_registry, tag="fast")
        assert {a.provider_name for a in ads} == {"p1", "p3"}

    def test_filters_by_node_id(self, tmp_path):
        svc = _service(tmp_path)
        publish_providers(
            svc, [_provider(name="p1")], node_id="example-host", principal_id="ben"
        )
        publish_providers(
            svc, [_provider(name="p2")], node_id="atlas", principal_id="ben"
        )
        ads = list_advertisements(svc.artifact_registry, node_id="example-host")
        assert {a.provider_name for a in ads} == {"p1"}

    def test_excludes_stale_advertisements_past_cutoff(self, tmp_path):
        """Advertisements older than `fresher_than` are excluded.

        Phase 1 doesn't yet implement refresh, but the catalog reader must
        already respect a freshness cutoff so downstream routers (Phase 2+)
        never see stale capability claims.
        """
        svc = _service(tmp_path)
        # Inject a backdated advertisement directly (simulating a peer whose
        # clock advertised capability 3h ago)
        stale_ts = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        stale_content = build_advertisement_content(_provider(name="stale"), node_id="example-host")
        stale_content["advertised_at"] = stale_ts
        svc.write(
            content=stale_content,
            cognitive_type="episodic",
            principal_id="ben",
            agents=set(),
            resources=set(),
        )
        # Fresh advertisement now
        publish_providers(
            svc, [_provider(name="fresh")], node_id="example-host", principal_id="ben"
        )

        cutoff = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        ads = list_advertisements(svc.artifact_registry, fresher_than=cutoff)
        assert {a.provider_name for a in ads} == {"fresh"}


class TestProviderAdvertisementShape:
    def test_preserves_fragment_id_for_audit_backlink(self, tmp_path):
        svc = _service(tmp_path)
        ids = publish_providers(
            svc, [_provider()], node_id="example-host", principal_id="ben"
        )
        ads = list_advertisements(svc.artifact_registry)
        assert ads[0].fragment_id == ids[0]

    def test_preserves_signature_when_present(self, tmp_path):
        svc = _service(tmp_path)
        publish_providers(svc, [_provider()], node_id="example-host", principal_id="ben")
        ads = list_advertisements(svc.artifact_registry)
        assert ads[0].signature is not None
        assert len(ads[0].signature) > 0
