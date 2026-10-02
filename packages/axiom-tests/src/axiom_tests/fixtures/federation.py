# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``mock_federation`` fixture — in-process fake federation peer.

Provides a lightweight stand-in for the Vega federation surface so that
extension tests can exercise cross-node scenarios without bringing up
real peers. The fake supports:

- Registering peer identities
- Publishing signed manifests
- Retrieving published manifests
- Simulating trust-profile lookups
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

import pytest


@dataclass
class FederationPeer:
    """A registered federation peer (by principal identity)."""

    principal: str
    trust_profile: str = "standard"
    classification_ceiling: str = "public"


@dataclass
class PublishedArtifact:
    """A manifest + fake signature published into the federation fabric."""

    name: str
    version: str
    publisher: str
    manifest: dict[str, Any]
    signature: str
    artifact_id: str


class MockFederation:
    """In-process fake federation fabric for tests."""

    def __init__(self) -> None:
        self.peers: dict[str, FederationPeer] = {}
        self.published: dict[str, PublishedArtifact] = {}
        self.events: list[tuple[str, dict[str, Any]]] = []

    # ---- Peer management -------------------------------------------------

    def register_peer(
        self,
        principal: str,
        *,
        trust_profile: str = "standard",
        classification_ceiling: str = "public",
    ) -> FederationPeer:
        peer = FederationPeer(
            principal=principal,
            trust_profile=trust_profile,
            classification_ceiling=classification_ceiling,
        )
        self.peers[principal] = peer
        self.events.append(("peer_registered", {"principal": principal}))
        return peer

    def get_peer(self, principal: str) -> FederationPeer | None:
        return self.peers.get(principal)

    # ---- Artifact distribution ------------------------------------------

    def publish(
        self,
        *,
        name: str,
        version: str,
        publisher: str,
        manifest: dict[str, Any],
    ) -> PublishedArtifact:
        if publisher not in self.peers:
            raise ValueError(
                f"publisher {publisher!r} has not been registered as a peer; "
                "call register_peer() first"
            )
        digest_input = f"{name}:{version}:{publisher}".encode()
        signature = hashlib.sha256(digest_input).hexdigest()
        artifact = PublishedArtifact(
            name=name,
            version=version,
            publisher=publisher,
            manifest=dict(manifest),
            signature=signature,
            artifact_id=str(uuid.uuid4()),
        )
        self.published[f"{name}@{version}"] = artifact
        self.events.append(("artifact_published", {"name": name, "version": version}))
        return artifact

    def fetch(self, name: str, version: str) -> PublishedArtifact | None:
        return self.published.get(f"{name}@{version}")

    # ---- Trust-profile queries -----------------------------------------

    def resolve_trust(self, principal: str) -> str:
        peer = self.peers.get(principal)
        return peer.trust_profile if peer else "unknown"

    def reset(self) -> None:
        self.peers.clear()
        self.published.clear()
        self.events.clear()


@pytest.fixture
def mock_federation() -> MockFederation:
    """Provide a fresh ``MockFederation`` for each test."""
    return MockFederation()


__all__ = ["FederationPeer", "MockFederation", "PublishedArtifact", "mock_federation"]
