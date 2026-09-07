# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Build a conversational *presence* agent for a comms channel.

The presence is the agent a human talks to in Slack/SMS/email — "Ben's Axi".
This module is the single, testable construction seam every surface (the live
`axi_presence.py` script today, a per-principal pod tomorrow) goes through, so
identity, branding, and host wiring all bind in one place rather than being
duplicated in scripts.

A0 (this): extract construction + persona/brief as config. Later phases bind a
principal (A1), deploy per-principal (A2), and add the laptop bridge (A3) by
extending `PresenceConfig`/`build_presence_agent` — not by editing scripts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Default deployment brief for the example sysadmin presence. Passed as data so a
# per-principal/per-site presence can supply its own without code edits. Instruct
# models honor an in-turn directive more reliably than a system persona alone, so
# the runner also seeds this into the first turn (see `axi_presence.py`).
DEFAULT_PRESENCE_BRIEF = (
    "You are Axi, the Axiom sysadmin agent present in the #example-host-sysadmin "
    "Slack channel. You tend a self-hosted node: a single-node k3d Kubernetes cluster "
    "(cluster axi-local) running the Axiom data platform (Dagster bronze ingest, "
    "pgvector, ollama nomic-embed) and a langfuse stack; you reach it via kubectl. "
    "Your model is Qwen3.5 served on that node itself. When asked who you are, you are "
    "Axi (the operator's agent), not 'generic Qwen' and not the Axiom computer-algebra "
    "system. Be concrete and operational; for changes, propose a reversible plan and "
    "ask for approval rather than claiming you already acted."
)


@dataclass
class PresenceConfig:
    """How a presence agent is constructed (data, not code).

    ``brief`` is the situational identity/deployment context. ``interaction_mode``
    is the chat-agent mode (``ask`` = conversation only, no tools; ``agent`` =
    full tool loop, used once C2 lets the presence run work). ``session_mode``
    ``public`` keeps general chat off the EC/VPN-gated routing path (#545).
    """

    brief: str = DEFAULT_PRESENCE_BRIEF
    interaction_mode: str = "ask"
    session_mode: str = "public"


def persona_for(rag_used: bool) -> str:
    """The agent's channel persona base name: **Neut** when an answer draws on
    the domain RAG, **Axi** otherwise (the identity rule). The owner possessive
    wraps this — see ``presence_display_name``."""
    return "Neut" if rag_used else "Axi"


def _context_of(handle: str) -> str | None:
    """Validate a principal handle and return its ``:context`` (``@axi:bens`` →
    ``"bens"``). Uses the real Principal validator (key bytes unused here)."""
    from axiom.vega.identity.principal import Principal

    return Principal(handle, b"").context


def presence_display_name(
    principal_handle: str,
    *,
    rag_used: bool = False,
    settings: Any | None = None,
    peers: Any | None = None,
    local_context: str | None = None,
    host: str | None = None,
) -> str:
    """The channel display name for a principal-bound presence — e.g. "Ben's Axi"
    (or "Alice's Axi" for a peer's agent, "…'s Neut" when RAG-backed).

    Owner token comes from ``resolve_owner_display`` (peer registry → user.name →
    birth-host). ``local_context`` defaults to the agent's own context (self-owned
    → local owner); pass this node's context to brand a *peer's* agent. ``settings``
    loads the real ``SettingsStore`` when omitted; tests inject fakes."""
    from axiom.extensions.builtins.notifications.owner_resolution import resolve_owner_display

    ctx = _context_of(principal_handle)
    if settings is None:
        try:
            from axiom.extensions.builtins.settings.store import SettingsStore

            settings = SettingsStore()
        except Exception:
            settings = None
    owner = resolve_owner_display(
        ctx, local_context=local_context or ctx, settings=settings, peers=peers, host=host
    )
    return f"{owner}'s {persona_for(rag_used)}"


def principal_slug(principal: str) -> str:
    """K8s-safe per-principal name: ``@axi:bens`` → ``axi-bens`` (release / SA /
    secret name for the per-principal pod, A2)."""
    ctx = _context_of(principal) or "local"
    return f"axi-{ctx}"


def secret_ref_for_principal(principal: str, key: str, *, namespace: str = "axiom-data") -> str:
    """Per-principal keystore path: one K8s Secret per principal, so each pod
    sees only its own creds. ``@axi:bens`` + ``SLACK_BOT_TOKEN`` →
    ``kubernetes://axiom-data/axi-bens/SLACK_BOT_TOKEN`` (A2)."""
    return f"kubernetes://{namespace}/{principal_slug(principal)}/{key}"


def build_presence_agent(
    config: PresenceConfig | None = None,
    *,
    principal: str | None = None,
    accountable_human_id: str | None = None,
    gateway: Any | None = None,
    bus: Any | None = None,
    session: Any | None = None,
) -> Any:
    """Construct a ``ChatAgent`` wired for a comms-channel presence, optionally
    bound to a ``principal`` (``@axi:bens``) accountable to a human.

    Binding populates ``session.principal_id``/``accountable_human_id`` — the seam
    ChatAgent already reads for memory provenance. Injectables default-construct
    when omitted; chat-layer imports are lazy so importing ``connect`` doesn't
    require the chat extension.
    """
    cfg = config or PresenceConfig()

    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.infra.bus import EventBus
    from axiom.infra.orchestrator.session import SessionStore
    from axiom.llm.gateway import Gateway

    sess = session if session is not None else SessionStore().create()
    if principal is not None:
        _context_of(principal)  # validate handle shape (raises on bad form)
        sess.principal_id = principal
    if accountable_human_id is not None:
        sess.accountable_human_id = accountable_human_id

    agent = ChatAgent(gateway=gateway or Gateway(), bus=bus or EventBus(), session=sess)
    agent.set_interaction_mode(cfg.interaction_mode)
    agent._workspace_context = cfg.brief
    agent._session_mode = cfg.session_mode
    return agent


__all__ = [
    "DEFAULT_PRESENCE_BRIEF",
    "PresenceConfig",
    "persona_for",
    "presence_display_name",
    "principal_slug",
    "secret_ref_for_principal",
    "build_presence_agent",
]
