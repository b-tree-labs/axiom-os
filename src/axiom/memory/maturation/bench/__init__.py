# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Maturation-aware memory benchmarks.

bench-1 (LongMemEval) is the first public benchmark wired through the
maturation pipeline. The harness lives in
:mod:`axiom.memory.maturation.bench.longmemeval` and is invokable as a
Python module or via ``axi memory bench longmemeval`` (CLI surface to
follow).

Two-run comparison per `prd-memory.md §3` axis 3:

- **Baseline** — episodic-only retrieval (no consolidation pass run)
- **Matured** — full maturation pipeline applied before queries

Whichever scores higher (and by how much) is the headline number that
defends the "memory matures and that matters" claim.
"""

from .longmemeval import (
    BenchmarkResult,
    LongMemEvalRunner,
    QuestionResult,
    SyntheticCorpus,
    load_corpus_from_huggingface,
    score_answer,
)

__all__ = [
    "BenchmarkResult",
    "LongMemEvalRunner",
    "QuestionResult",
    "SyntheticCorpus",
    "load_corpus_from_huggingface",
    "score_answer",
]
