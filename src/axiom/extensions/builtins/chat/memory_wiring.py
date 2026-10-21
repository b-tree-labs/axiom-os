# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Wire long-term memory into a bare ``axi chat`` agent.

When the user has run ``axi federation init`` (or any equivalent that
persisted a node identity), every chat session belongs to a principal.
This module bootstraps a ``CompositionService`` for that principal and
attaches it to the chat agent so:

- Each turn writes a ``prompt_composition`` episodic fragment via the
  agent's existing observability hook.
- The ``inject_session_memory`` cascade in ``agent._build_system_prompt``
  pulls prior-session episodic fragments back into the system prompt,
  giving cross-session continuity.
- ``axi memory show <principal>`` returns real fragments instead of
  "_No prior fragments_".

Without an identity (fresh install, classroom-only flow, etc.) the
function is a no-op — chat keeps working in stateless mode, and the
classroom path retains its own composition wiring.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .agent import ChatAgent

log = logging.getLogger(__name__)


def _principal_id_from_identity() -> str | None:
    """Derive a Matrix-style ``@name:context`` principal from local identity."""
    try:
        from axiom.vega.federation.identity import load_identity
    except Exception:
        return None
    try:
        identity = load_identity()
    except Exception as exc:  # noqa: BLE001 - a damaged identity must not stop chat
        log.warning("attach_memory: identity unreadable, chat runs stateless: %s", exc)
        return None
    if identity is None:
        return None
    display = identity.display_name or ""
    if not display:
        return None
    # display_name is conventionally already ``name:context`` (e.g. "ben:laptop").
    # Normalise to the @name:context form per feedback_principal_naming.
    return display if display.startswith("@") else f"@{display}"


def _build_user_composition():
    """Build a CompositionService rooted at ``get_user_state_dir() / "memory"``.

    Mirrors ``axi memory show``'s ``_build_default_composition`` so chat writes
    and ``axi memory show`` reads share one ArtifactRegistry. Without this
    alignment chat fragments land in ``runtime/extensions/chat/`` while
    ``axi memory show`` walks the user-state dir — same code, different
    SQLite files, no fragments visible.
    """
    from axiom.infra.paths import get_user_state_dir
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.keyfile import load_or_create_scope_key
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.recall import RecallIndex
    from axiom.memory.stores import open_ledger, open_recall_store
    from axiom.memory.trust import TrustGraph

    base = get_user_state_dir() / "memory"
    kp = load_or_create_scope_key(base)

    reg = open_ledger("user", base)
    audit = AuditLog(base / "audit.jsonl", signing_keypair=kp)
    # The recall corpus the axiom-memory MCP server reads and writes (same base, same store), so a turn
    # chat records is recallable from any harness and chat can recall what any harness recorded.
    store = open_recall_store(base)
    store.connect()
    return CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
        recall_index=RecallIndex(store=store),
    )


def attach_memory(agent: ChatAgent) -> bool:
    """Bootstrap a ``CompositionService`` for the chat agent's principal.

    Returns True iff memory was wired (identity found, CompositionService
    built, agent + session attributes set). Returns False on graceful
    degradation.

    The CompositionService is rooted at ``get_user_state_dir() / "memory"``
    — the same ArtifactRegistry ``axi memory show`` reads from — so per-turn
    fragments are immediately visible to the inspect command. (See
    ``axi memory show``'s ``_build_default_composition`` — both helpers MUST
    point at the same store; if you change one, change both.)
    """
    principal_id = _principal_id_from_identity()
    if not principal_id:
        log.debug("attach_memory: no identity → chat runs stateless")
        return False
    try:
        composition = _build_user_composition()
    except Exception as exc:
        log.warning("attach_memory: composition build failed: %s", exc)
        return False

    agent._composition = composition  # type: ignore[attr-defined]
    # Session is a dataclass without a principal_id field; setting an
    # attribute is fine (transient — re-resolved on resume).
    try:
        agent.session.principal_id = principal_id  # type: ignore[attr-defined]
    except Exception:
        log.debug("attach_memory: could not set session.principal_id")
    return True


# ---- the serving face --------------------------------------------------------------------------------

CHAT_HARNESS = "axiom-chat"


def session_principal(agent: ChatAgent) -> str:
    """Whose memory this chat reads: the session's principal, else the pinned ``memory.default_principal``
    (what the MCP server falls back to). Raises ``ValueError`` with the fix when neither exists."""
    principal = getattr(agent.session, "principal_id", "") or ""
    if principal:
        return principal
    from axiom.memory.session_capture import resolve_principal_id

    return resolve_principal_id(None)


def composition_for(agent: ChatAgent):
    """The agent's composition, built on first use if ``attach_memory`` did not wire one.

    Raises ``MemoryStoreUnavailable`` (with its advice) when the database is not reachable; the caller
    reports that sentence, never a stack trace.
    """
    composition = getattr(agent, "_composition", None)
    if composition is None:
        composition = _build_user_composition()
        agent._composition = composition  # type: ignore[attr-defined]
    return composition


def serving_service_for(agent: ChatAgent):
    """The same ``MemoryServingService`` the MCP ``axiom_memory_recall`` tool uses, over this chat's
    composition: recall -> fail-closed gate -> byte-stable rendering. One door."""
    from axiom.memory.serving import ServingGate
    from axiom.memory.serving_service import MemoryServingService

    return MemoryServingService(composition=composition_for(agent), gate=ServingGate())


def consumer_for(agent: ChatAgent, principal: str):
    """Who is asking and from where: this chat, as this principal, on the provider that will see the text.

    ``deployment_tier`` is ``local`` only when the active provider runs on this machine; a cloud or
    remote-node provider (or none resolvable) is ``remote``, so content restricted to the owned tier is
    denied by the gate rather than guessed about.
    """
    from axiom.memory.serving import TIER_LOCAL, TIER_REMOTE, ConsumerCoordinate

    from .where import where_report

    report = where_report(agent)
    local = bool(report.provider) and report.placement.placement == "local"
    return ConsumerCoordinate(
        principal=principal,
        harness=CHAT_HARNESS,
        account=principal,
        deployment_tier=TIER_LOCAL if local else TIER_REMOTE,
        model_endpoint=report.model,
    )


__all__ = [
    "CHAT_HARNESS",
    "attach_memory",
    "composition_for",
    "consumer_for",
    "serving_service_for",
    "session_principal",
]
