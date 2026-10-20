# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-030 Phase 1: federated inference capability catalog.

Phase 1 is **read-only advertisement only** — no routing, no policy
enforcement, no request/response audit records. Nodes publish one
``federated_provider`` MemoryFragment per exposed LLM provider;
consumers enumerate them via :func:`list_advertisements`.

ADR-029 four-primitives conformance:

1. Identity — advertisements are signed MemoryFragments (same identity
   roots as federated memory); the ``axiom://`` URI identifies
   ``<node>/inference/<provider_name>``.
2. Trust — no trust-weighted routing yet (Phase 3).
3. Policy — no policy enforcement yet (Phase 2 introduces
   requester/provider-side policy).
4. Content-addressed records — every advertisement is a
   MemoryFragment; consumers verify via the same signature path used
   for federated memory.

Phase 2 will add ``axi federation inference route`` + request/response
fragments. Phase 3 will add automatic routing with trust-weighted
ranking.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from axiom.artifacts.registry import ArtifactRegistry
    from axiom.infra.gateway import LLMProvider
    from axiom.memory.composition import CompositionService


FACT_KIND = "federated_provider"


@dataclass(frozen=True)
class ProviderAdvertisement:
    """One federated provider's advertised capability, as seen by a consumer."""

    node_id: str
    provider_name: str
    provider_uri: str  # axiom://<node_id>/inference/<provider_name>
    model: str
    routing_tier: str
    routing_tags: tuple[str, ...]
    requires_vpn: bool
    advertised_at: str  # ISO 8601, timezone-aware
    fragment_id: str
    signature: str | None  # hex Ed25519, or None if unsigned


def build_advertisement_content(provider: LLMProvider, *, node_id: str) -> dict[str, Any]:
    """Build the content dict for a ``federated_provider`` fragment.

    The shape is what the Phase 2 router will consume for ranking, so
    keep it stable across Phase 1 publishers.
    """
    now = datetime.now(UTC).isoformat()
    return {
        "fact_kind": FACT_KIND,
        "event_time": now,  # required by episodic content contract
        "node_id": node_id,
        "provider_name": provider.name,
        "provider_uri": f"axiom://{node_id}/inference/{provider.name}",
        "model": provider.model,
        "routing_tier": provider.routing_tier,
        "routing_tags": list(provider.routing_tags),
        "requires_vpn": bool(provider.requires_vpn),
        "advertised_at": now,
    }


def publish_providers(
    composition: CompositionService,
    providers: list[LLMProvider],
    *,
    node_id: str,
    principal_id: str,
) -> list[str]:
    """Publish a ``federated_provider`` fragment per provider.

    Returns the list of written fragment IDs in the same order as
    ``providers``. Writes route through :class:`CompositionService` so
    they inherit signing, policy, and audit. Callers wishing to refresh
    an existing advertisement simply publish again — each advertisement
    is timestamped and the consumer filters by ``fresher_than``.
    """
    ids: list[str] = []
    for p in providers:
        content = build_advertisement_content(p, node_id=node_id)
        frag = composition.write(
            content=content,
            cognitive_type="episodic",
            principal_id=principal_id,
            agents=set(),
            resources=set(),
        )
        ids.append(frag.id)
    return ids


def list_advertisements(
    artifact_registry: ArtifactRegistry,
    *,
    node_id: str | None = None,
    tier: str | None = None,
    tag: str | None = None,
    fresher_than: str | None = None,
) -> list[ProviderAdvertisement]:
    """Enumerate ``federated_provider`` advertisements in the registry.

    Filters compose (all must hold). ``fresher_than`` is an ISO-8601
    cutoff; advertisements with ``advertised_at < fresher_than`` are
    excluded. Lexicographic comparison is correct for ISO-8601 UTC.
    """
    ads: list[ProviderAdvertisement] = []
    for artifact in artifact_registry.list(kind="fragment"):
        data = artifact.data
        content = data.get("content", {})
        if content.get("fact_kind") != FACT_KIND:
            continue

        if node_id is not None and content.get("node_id") != node_id:
            continue
        if tier is not None and content.get("routing_tier") != tier:
            continue
        if tag is not None and tag not in content.get("routing_tags", []):
            continue
        advertised_at = content.get("advertised_at", "")
        if fresher_than is not None and advertised_at < fresher_than:
            continue

        ads.append(
            ProviderAdvertisement(
                node_id=content.get("node_id", ""),
                provider_name=content.get("provider_name", ""),
                provider_uri=content.get("provider_uri", ""),
                model=content.get("model", ""),
                routing_tier=content.get("routing_tier", "any"),
                routing_tags=tuple(content.get("routing_tags", [])),
                requires_vpn=bool(content.get("requires_vpn", False)),
                advertised_at=advertised_at,
                fragment_id=data.get("id", artifact.name),
                signature=data.get("signature"),
            )
        )
    return ads
