# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Axiom eval harness.

An eval is a (suite of cases) × (set of scorers) × (runner). The harness
runs the runner over each case, applies every scorer, aggregates scores,
and emits traces to a TraceProvider. Reports indicate pass/fail against
per-scorer thresholds.

Slice 2 (Hello Evals) of Phase 0.
"""

from __future__ import annotations

from axiom.evals.harness import EvalCase, EvalHarness, EvalReport

__all__ = ["EvalCase", "EvalHarness", "EvalReport"]
