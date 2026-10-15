# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Axiom research loop.

Karpathy-style iterative research: formulate → run → evaluate → refine →
repeat until score meets threshold or iteration budget exhausts. The loop
itself is backend-agnostic — you inject a runner (anything that answers a
question), a scorer, and a refiner. Classroom auto-research, CURIO
cross-node synthesis, and eval-gated knowledge promotion all drive this
engine with different runners.

Slice 3 of Phase 0.
"""

from __future__ import annotations

from axiom.research.loop import ResearchLoop, ResearchQuestion, ResearchResult

__all__ = ["ResearchLoop", "ResearchQuestion", "ResearchResult"]
