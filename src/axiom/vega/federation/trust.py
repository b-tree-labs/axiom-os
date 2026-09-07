# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Trust bootstrap — invitation-based federation joining."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from axiom.vega.federation.identity import NodeIdentity, NodeManifest


@dataclass
class InvitationToken:
    """A time-limited invitation for a remote node to join the federation."""

    token: str
    issuer_node_id: str
    issuer_display_name: str
    created_at: str  # ISO 8601
    expires_at: str  # ISO 8601, 24 h TTL by default
    accepted: bool = False
    accepted_by: str = ""

    def is_expired(self) -> bool:
        """Return ``True`` if the token has passed its expiry time."""
        now = datetime.now(UTC)
        exp = datetime.fromisoformat(self.expires_at)
        return now >= exp

    def to_dict(self) -> dict:
        """Serialise to a plain ``dict``."""
        return {
            "token": self.token,
            "issuer_node_id": self.issuer_node_id,
            "issuer_display_name": self.issuer_display_name,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "accepted": self.accepted,
            "accepted_by": self.accepted_by,
        }


def create_invitation(
    identity: NodeIdentity,
    ttl_hours: int = 24,
) -> InvitationToken:
    """Create a new invitation token signed by *identity*."""
    now = datetime.now(UTC)
    expires = now + timedelta(hours=ttl_hours)
    raw = secrets.token_urlsafe(32)
    return InvitationToken(
        token=raw,
        issuer_node_id=identity.node_id,
        issuer_display_name=identity.display_name,
        created_at=now.isoformat(),
        expires_at=expires.isoformat(),
    )


def validate_invitation(token_str: str, issuer_manifest: NodeManifest) -> bool:
    """Basic validation: token is non-empty and issuer manifest has a node_id.

    Real implementations will verify a cryptographic signature over the token
    using the issuer's public key.  This stub checks structural validity only.
    """
    if not token_str or not issuer_manifest.node_id:
        return False
    return True
