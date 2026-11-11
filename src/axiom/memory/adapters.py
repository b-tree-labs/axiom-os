# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Adapter bridges from extension-bespoke stores to the L1 memory layer.

ADR-033 Stage 1. Extensions whose data morally belongs in L1 (memorable,
federable, classifiable, retractable) but historically lives in their
own store can opt into dual-write through these adapters. Each adapter
translates the extension's record shape into a ``MemoryFragment``
written via ``CompositionService``; the extension's bespoke store
remains the read source until Stage 4 promotes L1 to canonical.

Stage 1 is intentionally minimal:

- One adapter factory per source store type (``interaction_writer`` here).
- The extension stays in control of *whether* to dual-write — the
  factory returns a callable; the store accepts an ``Optional`` of
  that callable; default is ``None`` and behaviour is unchanged.
- Failures in the L1 write are intentionally swallowed by the store
  during migration; the JSONL stays authoritative. Stage 4 promotes
  L1 to the source of truth and tightens the failure mode.
- Tombstone propagation lands in Stage 1.5 once the L1 tombstone
  primitive (separate from the classroom-level tombstone) is in
  place. Tracked in ADR-033 open questions.

Each adapter is the *only* place in the codebase that knows how to map
its source record to a ``MemoryFragment``. Keeping that mapping
centralized makes the migration auditable: when L1 becomes canonical,
the migration helper reads the same mapping in reverse.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from axiom.extensions.builtins.classroom.classroom_interaction import (
        InteractionRecord,
    )

    from .composition import CompositionService


# ---------------------------------------------------------------------------
# Classroom interaction adapter
# ---------------------------------------------------------------------------


def interaction_writer(
    composition_service: CompositionService,
) -> Callable[[InteractionRecord, str], None]:
    """Build the dual-write callable for ``ClassroomInteractionStore``.

    The returned function takes an ``InteractionRecord`` + scope id
    (the classroom_id) and writes a corresponding ``MemoryFragment``
    via ``CompositionService``.

    Mapping decisions, fixed at this seam:

    - ``cognitive_type = episodic`` — every interaction is a thing
      that happened at a point in time. ``content["event_time"]`` is
      required by the EPISODIC validator.
    - ``content`` carries the question, answer-status, citation count,
      mode, classroom_id, interaction_id (the deterministic hash from
      InteractionRecord.interaction_id). The fragment_id is generated
      separately by ``CompositionService``; the interaction_id stays
      in content so reverse-lookups (forget, audit) work without a
      separate index.
    - ``principal_id = student_id`` — the student is the contributing
      user per (T,U,A,R).
    - ``agents = empty`` — no automated agent involvement at write
      time (the LLM that will project this into context comes later).
    - ``resources = empty`` for v0; later extensions can include
      retrieved-citation fragment ids here.
    - ``visibility`` and ``classification`` are left at fragment
      defaults (SCOPE_INTERNAL + unclassified) — student questions
      are scope-private by default per the federation policy spec.
      Extensions wanting different defaults can write directly through
      CompositionService and bypass this adapter.
    """
    def _writer(record: InteractionRecord, scope_id: str) -> None:
        composition_service.write(
            content={
                "event_time": record.timestamp,
                "interaction_id": record.interaction_id,
                # ``scope`` is the recommended generic content key per
                # spec-memory §3 (until MemoryFragment gains a top-level
                # scope field — open question §14). ``classroom_id`` is
                # kept as a backward-compat alias for the canary
                # extension's bespoke reads; both forms point at the
                # same value so projections find scope under either key.
                "scope": scope_id,
                "classroom_id": scope_id,
                "question": record.question,
                "had_answer": record.had_answer,
                "citations_count": record.citations_count,
                "mode": record.mode or "ask",
            },
            cognitive_type="episodic",
            principal_id=record.student_id,
            agents=set(),
            resources=set(),
        )

    return _writer


__all__ = ["interaction_writer"]
