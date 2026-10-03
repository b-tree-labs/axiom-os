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
    identity = load_identity()
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
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.infra.paths import get_user_state_dir
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import Keypair, generate_keypair

    base = get_user_state_dir() / "memory"
    base.mkdir(parents=True, exist_ok=True)
    key_path = base / "node.key"
    if key_path.exists():
        kp = Keypair.from_private_bytes(key_path.read_bytes())
    else:
        kp = generate_keypair()
        key_path.write_bytes(kp.export_private())

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


__all__ = ["attach_memory"]
