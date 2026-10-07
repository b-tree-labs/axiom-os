# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``axiom.llm`` — the first-class LLM core primitive (ADR-071).

The coherent home for LLM serving, routing, tiers, provisioning, and health
across Axiom — peer to ``axiom.memory`` / ``axiom.identity``, consumed by the
``rag`` extension and ~11 others through a single gateway seam. They never own
serving; swapping a model is a provider/provisioning concern, not a consumer
change. Dependency direction is one-way (extensions → core ``llm``), per
ADR-070.

This package is being assembled incrementally (epic axiom-os#506). The first
member is :mod:`axiom.llm.health` — the model-coherence gate (#499) that would
have caught a degenerate served model in minutes rather than 68 days. The
gateway / router / params / tiers / provisioning consolidation follows behind
back-compat shims.
"""

from __future__ import annotations

from axiom.llm.health import (
    CoherenceProbe,
    CoherenceReport,
    CoherenceScore,
    ProbeResult,
    check_model_coherence,
    score_coherence,
)

__all__ = [
    "CoherenceProbe",
    "CoherenceReport",
    "CoherenceScore",
    "ProbeResult",
    "check_model_coherence",
    "score_coherence",
]
