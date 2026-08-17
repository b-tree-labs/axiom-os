# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Corpus retrieval evaluation — coverage-aware scoring that *knows what to
look for*, plus the taxonomy-cluster derivation used for routed retrieval.

Two jobs, both pure + unit-testable (the retriever is injected, so no DB/model):

1. :func:`cluster_of` — derive a logical cluster id from a chunk's
   ``source_path``. The source folder taxonomy (e.g. a curated literature
   library) is preserved verbatim in ``source_path``; the cluster is the first
   ``depth`` folder segments. Expert-curated boundaries make better retrieval
   partitions than auto-clustering, and this needs no re-ingest.

2. :func:`score_retrieval` — run a question set through a retriever and score
   **citation coverage** (did the expected source come back?) overall and
   **per cluster**, so a partial/uneven corpus is detected by *which clusters
   miss*, not just an aggregate. This is the judge for the flat-vs-routed-vs-
   boosted A/B (does the taxonomy lift recall?).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field


def cluster_of(source_path: str, *, depth: int = 2) -> str:
    """Logical cluster id = the first ``depth`` folder segments of the path.

    ``/_____Literature_____/MSR/MSRE/openmsr/ornl/x.txt`` → ``_____Literature_____/MSR``
    (depth 2). Returns ``""`` for empty paths. Leading/trailing slashes ignored.
    """
    segs = [s for s in source_path.split("/") if s]
    return "/".join(segs[:depth])


@dataclass
class QuestionResult:
    qid: str
    cluster_tags: list[str]
    cited: bool | None          # None when the question has no expected_citations
    top_sim: float
    top_path: str


@dataclass
class CoverageReport:
    results: list[QuestionResult] = field(default_factory=list)
    # cluster -> (passed, total) over citation-bearing questions
    by_cluster: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def cited_total(self) -> int:
        return sum(1 for r in self.results if r.cited is not None)

    @property
    def cited_passed(self) -> int:
        return sum(1 for r in self.results if r.cited)

    @property
    def coverage(self) -> float:
        n = self.cited_total
        return (self.cited_passed / n) if n else 1.0

    @property
    def mean_top_sim(self) -> float:
        return (sum(r.top_sim for r in self.results) / len(self.results)) if self.results else 0.0


def score_retrieval(
    questions: Sequence[dict],
    retrieve: Callable[[str], list[tuple[str, float]]],
) -> CoverageReport:
    """Score a question set against ``retrieve(question) -> [(source_path, sim)]``.

    A question with ``expected_citations`` passes iff *every* expected substring
    appears in some returned path (the eval set uses path substrings). Coverage
    is tracked overall and per cluster (from the question's ``tags`` that name a
    subfolder, falling back to the cluster of the top hit).
    """
    report = CoverageReport()
    cluster_counts: dict[str, list[int]] = {}
    for q in questions:
        hits = retrieve(q["question"])
        paths = [p for p, _ in hits]
        top_path, top_sim = (hits[0][0], hits[0][1]) if hits else ("", 0.0)
        expected = q.get("expected_citations") or []
        cited: bool | None
        cited = all(any(e in p for p in paths) for e in expected) if expected else None
        tags = list(q.get("tags") or [])
        report.results.append(QuestionResult(q["id"], tags, cited, top_sim, top_path))
        if cited is not None:
            key = next((t.split(":", 1)[1] for t in tags if t.startswith("subfolder:")), None)
            key = key or cluster_of(top_path)
            slot = cluster_counts.setdefault(key, [0, 0])
            slot[1] += 1
            if cited:
                slot[0] += 1
    report.by_cluster = {k: (v[0], v[1]) for k, v in cluster_counts.items()}
    return report


__all__ = ["cluster_of", "QuestionResult", "CoverageReport", "score_retrieval"]
