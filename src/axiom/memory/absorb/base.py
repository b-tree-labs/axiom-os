# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Absorb adapter seam (ADR-087 D8, PRD F3).

One adapter per harness memory model (markdown-hierarchy, local
structured store, vector/passage, cloud API). Adapters are **read-only**
against source stores: they scan, normalize, and yield
:class:`FragmentCandidate`s stamped with a write-once ``SourceOrigin``
coordinate — they never write anywhere. All writes land via the D2
import primitive (:func:`axiom.memory.absorb.importer.import_candidates`),
which goes through ``CompositionService`` — the single door in.

Schema drift in app-owned stores degrades to skip-with-audit
(:class:`SkippedSource` records in the scan), never a crash and never a
partial write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from axiom.memory.fragment import SourceOrigin


@dataclass(frozen=True)
class FragmentCandidate:
    """A normalized, not-yet-written memory read out of a source store.

    ``content`` is the eventual fragment content dict; ``cognitive_type``
    is a MIRIX type name (adapters emit ``semantic`` unless the source
    carries stronger typing); ``origin`` is the ADR-087 D1 coordinate —
    ``(harness, account, source_ref)`` is the idempotency key that
    suppresses re-absorb echo downstream.
    """

    content: dict[str, Any]
    cognitive_type: str
    origin: SourceOrigin


@dataclass(frozen=True)
class SkippedSource:
    """One source location an adapter (or the importer) could not use.

    ``source`` names the store/file/row; ``reason`` says why. Skips are
    surfaced in reports and audit-logged on import — degraded, never
    silent.
    """

    source: str
    reason: str


@dataclass
class AbsorbScan:
    """What one adapter scan produced: candidates + what it skipped."""

    candidates: list[FragmentCandidate] = field(default_factory=list)
    skipped: list[SkippedSource] = field(default_factory=list)


@runtime_checkable
class AbsorbAdapter(Protocol):
    """Structural contract every absorb adapter satisfies.

    ``harness`` is the origin-coordinate harness label the adapter
    stamps; ``scan()`` reads the source store (read-only) and returns
    an :class:`AbsorbScan`. Adapters hold no write handles.
    """

    harness: str

    def scan(self) -> AbsorbScan: ...


__all__ = [
    "AbsorbAdapter",
    "AbsorbScan",
    "FragmentCandidate",
    "SkippedSource",
]
