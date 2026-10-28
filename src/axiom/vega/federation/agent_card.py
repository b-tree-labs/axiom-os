# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A2A Agent Card — per Google Agent2Agent Protocol v0.3."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from axiom.vega.federation.identity import NodeIdentity


@dataclass
class AgentCard:
    """A2A-compliant Agent Card with Axiom extension fields."""

    name: str
    description: str
    url: str  # A2A endpoint URL
    icon_url: str = ""  # A2A `iconUrl` — the agent's avatar
    version: str = "0.1.0"
    capabilities: dict = field(
        default_factory=lambda: {"streaming": False, "pushNotifications": False}
    )
    skills: list[dict] = field(default_factory=list)
    # Axiom extensions
    axiom_node_id: str = ""
    axiom_profile: str = ""
    axiom_extensions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialise to a plain ``dict``."""
        return {
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "iconUrl": self.icon_url,
            "version": self.version,
            "capabilities": self.capabilities,
            "skills": self.skills,
            "axiom_node_id": self.axiom_node_id,
            "axiom_profile": self.axiom_profile,
            "axiom_extensions": self.axiom_extensions,
        }

    def to_json(self) -> str:
        """Serialise to a JSON string."""
        return json.dumps(self.to_dict(), indent=2)


def build_agent_card(
    identity: NodeIdentity,
    base_url: str = "http://localhost:8080",
) -> AgentCard:
    """Build an A2A Agent Card from a node's identity."""
    return AgentCard(
        name=identity.display_name,
        description=f"Axiom federation node owned by {identity.owner}",
        url=f"{base_url}/.well-known/agent-card.json",
        axiom_node_id=identity.node_id,
        axiom_profile=identity.profile,
    )


def build_agent_card_for_principal(
    principal: str,
    *,
    display_name: str,
    icon_url: str = "",
    identity: NodeIdentity | None = None,
    base_url: str = "http://localhost:8080",
) -> AgentCard:
    """Build a *per-user* Agent Card (e.g. "Ben's Axi") for a comms presence.

    Node-level ``build_agent_card`` describes the node; this describes the agent
    a specific human talks to — its possessive ``display_name`` and per-user
    ``icon_url`` (avatar). ``identity`` is optional (binds the owning node when
    present)."""
    return AgentCard(
        name=display_name,
        description=f"{display_name} — personal Axiom agent ({principal})",
        url=f"{base_url}/.well-known/agent-card.json",
        icon_url=icon_url,
        axiom_node_id=getattr(identity, "node_id", "") or "",
        axiom_profile=getattr(identity, "profile", "") or "",
    )
