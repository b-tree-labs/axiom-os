# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom-layer bootstrap for CompositionService.

Assembles a fully-wired CompositionService per classroom, with state
persisted at runtime/classrooms/<classroom_id>/. Idempotent — a second
call with the same classroom_id resumes existing state.

Defaults:
- Artifact registry: SQLite at runtime/classrooms/<id>/artifacts.db
- Audit log: JSONL at runtime/classrooms/<id>/audit.jsonl
- Signing keypair: auto-generated at first bootstrap, persisted at
  runtime/classrooms/<id>/node.key
- Policy coordinate: classroom-default profile (write=private)
- Access graphs: empty at bootstrap; wired as students enroll
- Trust graph: empty at bootstrap; seeded as trust records accumulate
- Transform: anonymize_principal when shared-tier writes occur
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend

if TYPE_CHECKING:
    from axiom.extensions.builtins.classroom.tracing import ClassroomTracer
from axiom.memory.access import AccessGraphs
from axiom.memory.attest import AuditLog
from axiom.memory.composition import CompositionService
from axiom.memory.policy import PolicyCoord, with_global
from axiom.memory.trust import TrustGraph
from axiom.memory.write_policy import anonymize_principal
from axiom.vega.identity.keypair import Keypair, generate_keypair


def _runtime_root() -> Path:
    override = os.environ.get("AXIOM_RUNTIME_ROOT")
    if override:
        return Path(override)
    try:
        from axiom import REPO_ROOT  # type: ignore

        return Path(REPO_ROOT) / "runtime"
    except Exception:
        return Path.cwd() / "runtime"


def _classroom_dir(classroom_id: str) -> Path:
    d = _runtime_root() / "classrooms" / classroom_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_or_generate_keypair(path: Path) -> Keypair:
    if path.exists():
        return Keypair.from_private_bytes(path.read_bytes())
    kp = generate_keypair()
    path.write_bytes(kp.export_private())
    # Restrict permissions on best-effort basis
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return kp


def _default_policy() -> PolicyCoord:
    """Classroom-default policy coordinate.

    Conservative defaults: writes private, explicit shared only for
    research-export paths. Extensions override at their call sites.
    """
    return with_global(
        PolicyCoord(),
        {
            "write": "private",
            "read": "allow",
        },
    )


def build_classroom_composition(classroom_id: str) -> CompositionService:
    """Bootstrap a fully-wired CompositionService for a classroom.

    Idempotent: a second call resumes existing state from disk.
    """
    cdir = _classroom_dir(classroom_id)

    registry = ArtifactRegistry(backend=SQLiteBackend(cdir / "artifacts.db"))
    keypair = _load_or_generate_keypair(cdir / "node.key")
    audit = AuditLog(cdir / "audit.jsonl", signing_keypair=keypair)

    return CompositionService(
        artifact_registry=registry,
        audit_log=audit,
        signing_keypair=keypair,
        policy_coord=_default_policy(),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
        transform=anonymize_principal,
    )


def build_classroom_tracer(
    classroom_id: str,
    course_id: str,
    composition: CompositionService | None = None,
) -> ClassroomTracer:
    """Bootstrap a ClassroomTracer with an env-configured provider.

    Returns a tracer wired to:
    - The env-selected trace provider (LangFuse if keys present, else
      null). See ``axiom.infra.tracing.env.build_trace_provider_from_env``.
    - An optional composition service for materializing traces as
      episodic fragments (the unified-memory path).

    For Prague deployments, a node operator sets ``LANGFUSE_PUBLIC_KEY``
    + ``LANGFUSE_SECRET_KEY`` (+ optional ``LANGFUSE_HOST``) and every
    classroom that calls this helper gets real observability without
    code changes.
    """
    from axiom.extensions.builtins.classroom.tracing import ClassroomTracer
    from axiom.infra.tracing.env import build_trace_provider_from_env

    provider = build_trace_provider_from_env()
    return ClassroomTracer(
        classroom_id=classroom_id,
        course_id=course_id,
        trace_provider=provider,
        composition=composition,
    )
