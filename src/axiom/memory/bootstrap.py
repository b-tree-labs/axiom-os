# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One-call bootstrap for any Axiom extension that needs memory.

Per ``prd-memory.md §6`` — the "*the* choice" positioning rests on
extension authors getting rich, fully-featured memory without effort.
This module is the affordance: ``build_memory_stack(scope_id)`` returns
a fully-wired ``MemoryStack`` with L1 (CompositionService + signed
audit log + content-addressed registry), L2 (concept graph +
deterministic text extractor), and L3-ready (RecentActivityProjection
ready to consume). Extensions wire one call into their bootstrap and
get every spec-memory invariant for free.

Convention: state lives at ``runtime/extensions/<scope_id>/`` by
default, override-able with ``data_root=`` for tests + alternative
deployments. Idempotent — second call resumes existing state.

Profile mapping (per ADR-019 node profiles):
- Edge / Workstation: SQLite for L1 + L2 (zero-deps)
- Server / Platform: subclass MemoryStack + override the registry +
  graph backends with Postgres / AGE / SeaweedFS as appropriate

The point: every extension that calls this gets the full stack —
provenance, classification, federation-readiness, retraction,
projections, concept graph — without thinking about it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
from axiom.memory.access import AccessGraphs
from axiom.memory.adapters import interaction_writer
from axiom.memory.attest import AuditLog
from axiom.memory.composition import CompositionService
from axiom.memory.graph import (
    DeterministicTextExtractor,
    ExtractorRegistry,
    SQLiteConceptGraph,
)
from axiom.memory.policy import PolicyCoord, with_global
from axiom.memory.projections import RecentActivityProjection
from axiom.memory.trust import TrustGraph
from axiom.memory.write_policy import anonymize_principal
from axiom.vega.identity.keypair import Keypair, generate_keypair


def _runtime_root() -> Path:
    """Match the existing classroom composition_boot convention so
    extensions land their data alongside everything else by default."""
    override = os.environ.get("AXIOM_RUNTIME_ROOT")
    if override:
        return Path(override)
    try:
        from axiom import REPO_ROOT  # type: ignore

        return Path(REPO_ROOT) / "runtime"
    except Exception:
        return Path.cwd() / "runtime"


def _scope_dir(scope_id: str, *, data_root: Path | None = None) -> Path:
    root = data_root if data_root is not None else _runtime_root() / "extensions"
    d = root / scope_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_or_generate_keypair(path: Path) -> Keypair:
    if path.exists():
        return Keypair.from_private_bytes(path.read_bytes())
    kp = generate_keypair()
    path.write_bytes(kp.export_private())
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return kp


@dataclass
class MemoryStack:
    """Fully-wired memory stack for an extension's scope.

    Holds every primitive the extension would otherwise wire by hand:
    L1 ``CompositionService`` (with audit log + signing keypair +
    artifact registry), L2 ``ConceptGraph`` + ``ExtractorRegistry``
    (with the deterministic text extractor pre-registered), and a
    helper that returns L3 ``RecentActivityProjection`` instances on
    demand. Adapters for common bespoke stores (interaction store,
    etc.) are exposed as factories.

    Extensions consume it by calling its methods rather than
    constructing memory primitives themselves. The "you can't get
    memory wrong" promise lives here.
    """

    scope_id: str
    composition: CompositionService
    graph: SQLiteConceptGraph
    extractors: ExtractorRegistry
    keypair: Keypair
    data_dir: Path

    # --- Convenience accessors ---------------------------------------------

    @property
    def artifact_registry(self) -> ArtifactRegistry:
        return self.composition.artifact_registry

    @property
    def audit_log(self) -> AuditLog:
        return self.composition.audit_log

    # --- Adapters --------------------------------------------------------

    def interaction_writer(self):
        """Return the dual-write callable for ``ClassroomInteractionStore``-
        shape stores (and any future store with the same shape)."""
        return interaction_writer(self.composition)

    # --- Projection factories -------------------------------------------

    def recent_activity(self, *, window_n: int = 5) -> RecentActivityProjection:
        """Return a ``RecentActivityProjection`` ready to project from
        the same artifact_registry the L1 dual-write writes into."""
        return RecentActivityProjection(
            artifact_registry=self.artifact_registry,
            window_n=window_n,
        )

    # --- Hook integration with CompositionService -----------------------

    def write_with_extraction(self, **write_kwargs):
        """Write through CompositionService AND run extractors.

        Convenience method that combines L1 write + L2 extraction in a
        single call. Returns the written ``MemoryFragment``. The
        extractor summary is available afterwards via
        ``extractors.run_for_fragment(fragment)`` for callers that want
        observability; this wrapper discards it for ergonomic use.
        """
        fragment = self.composition.write(**write_kwargs)
        # Async-safe by default: if a future async-extractor backend
        # ships, this stays inline-ok because the registry tolerates
        # extractors that queue their work (per spec-memory I13).
        self.extractors.run_for_fragment(fragment)
        return fragment


def build_memory_stack(
    scope_id: str,
    *,
    data_root: Path | None = None,
    register_default_extractors: bool = True,
    enable_signing: bool = True,
    enable_anonymize_transform: bool = True,
) -> MemoryStack:
    """Bootstrap the full memory stack for an extension scope.

    Idempotent: a second call with the same ``scope_id`` resumes
    existing state from disk (artifacts, audit log, signing keypair,
    concept graph all persist).

    Defaults match what classroom uses today, so any extension built on
    top gets the same provenance + classification + federation-readiness
    + concept-graph + projection layer — "industry-leading memory
    without effort" per prd-memory §6.

    Override knobs:

    - ``data_root``: state directory; defaults to
      ``$AXIOM_RUNTIME_ROOT/extensions/<scope_id>/`` (or
      ``runtime/extensions/<scope_id>/`` if no env override).
    - ``register_default_extractors``: when False, skip registering the
      built-in DeterministicTextExtractor (extensions that ship their
      own extractor with stricter capability declarations may want this).
    - ``enable_signing``: when False, the audit log won't sign entries.
      Kept as a knob for tests + niche profiles; default True.
    - ``enable_anonymize_transform``: when False, shared-tier writes
      don't pseudonymize principals. Kept for tests + extensions that
      want full-fidelity audit at the cost of shareable-projection
      defaults; default True.
    """
    sdir = _scope_dir(scope_id, data_root=data_root)

    keypair = _load_or_generate_keypair(sdir / "node.key")
    registry = ArtifactRegistry(backend=SQLiteBackend(sdir / "artifacts.db"))
    audit = AuditLog(
        sdir / "audit.jsonl",
        signing_keypair=keypair if enable_signing else None,
    )
    policy = with_global(
        PolicyCoord(),
        {"write": "private", "read": "allow"},
    )

    composition = CompositionService(
        artifact_registry=registry,
        audit_log=audit,
        signing_keypair=keypair if enable_signing else None,
        policy_coord=policy,
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
        transform=anonymize_principal if enable_anonymize_transform else None,
    )

    graph = SQLiteConceptGraph(sdir / "concepts.db")
    extractors = ExtractorRegistry(graph=graph)
    if register_default_extractors:
        extractors.register(DeterministicTextExtractor())

    return MemoryStack(
        scope_id=scope_id,
        composition=composition,
        graph=graph,
        extractors=extractors,
        keypair=keypair,
        data_dir=sdir,
    )


__all__ = ["MemoryStack", "build_memory_stack"]
