# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The gate a precision or dimensionality change has to clear.

Halving an embedding's width, or truncating it, frees a great deal of disk. It
also changes what comes back. Disk is far easier to measure than retrieval
quality, so the standing temptation is to report the easy number and call it a
result. A storage win that quietly costs recall is a regression.

Two modes, and the difference between them is the point.

**Labeled** (:func:`compare_recall`). A query set where the expected document is
known. Each arm either returns it in the top *k* or does not, over the same
queries — paired binary outcomes, which is exactly what McNemar's test is for.
It reuses :mod:`axiom.evals.significance`, the same test the comparative battery
uses, so a claim here means what a claim there means. **This mode can prove
quality held.**

**Agreement** (:func:`agreement`). No labels; the incumbent encoding's results
are the reference. It measures how far the new results drift. It can detect a
change and can **never** prove quality, because agreeing with the incumbent is
not the same as being right — if the incumbent was wrong, perfect agreement
reproduces the error. It says so in its own output, and the milestone requires
the labeled mode.

The gate refuses to pass on too few queries. Four queries that all agree is not
evidence recall held; it is an absence of evidence, and a gate that accepted it
would accept anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

#: Queries below which the comparison cannot support a conclusion. Chosen so a
#: single-digit discordant count still has some power; it is a floor, not a
#: target.
MIN_QUERIES = 30


@dataclass(frozen=True)
class QueryCase:
    """One query, its expected document, and what each arm returned."""

    query_id: Any
    expected: str
    before: Sequence[str]
    after: Sequence[str]


def hit_at_k(results: Sequence[str], *, expected: str, k: int) -> bool:
    """Whether the expected document is in the first ``k`` results."""
    return expected in list(results)[:k]


def compare_recall(
    cases: Sequence[QueryCase], *, k: int = 5, min_queries: int = MIN_QUERIES,
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Paired hit@k before against after, with McNemar deciding.

    Reports the counts behind the verdict, not only the verdict: a boolean
    invites quoting the conclusion and dropping the evidence, and the discordant
    count is what says whether a p-value rests on four queries or four hundred.
    """
    from axiom.evals.significance import mcnemar

    before_hits = [hit_at_k(c.before, expected=c.expected, k=k) for c in cases]
    after_hits = [hit_at_k(c.after, expected=c.expected, k=k) for c in cases]

    result = mcnemar(baseline=before_hits, candidate=after_hits, alpha=alpha)
    lost = sum(1 for b, a in zip(before_hits, after_hits) if b and not a)
    gained = sum(1 for b, a in zip(before_hits, after_hits) if a and not b)
    n = len(cases)

    regression = result.significant and lost > gained
    reasons: list[str] = []
    if n < min_queries:
        reasons.append(
            f"too few queries: {n} < {min_queries}. Agreement across a small set "
            f"is an absence of evidence, not evidence that recall held."
        )
    if regression:
        reasons.append(
            f"{lost} queries lost their expected document and {gained} gained one "
            f"(p={result.p_value:.4g}). This is a regression."
        )

    return {
        "queries": n,
        "k": k,
        "hit_rate_before": round(sum(before_hits) / n, 4) if n else None,
        "hit_rate_after": round(sum(after_hits) / n, 4) if n else None,
        "lost": lost,
        "gained": gained,
        "discordant": result.discordant,
        "p_value": result.p_value,
        "significant": result.significant,
        "favors": result.favors,
        "regression": regression,
        "passes_gate": not reasons,
        "note": " ".join(reasons),
        "mode": "labeled",
    }


def agreement(pairs: Sequence[tuple[Sequence[str], Sequence[str]]], *,
              k: int = 5) -> dict[str, Any]:
    """Overlap@k between two encodings, with no claim about quality.

    Use when no labeled query set exists. It detects drift and nothing more:
    perfect agreement with an incumbent that was wrong reproduces the error
    exactly, and would read here as a flawless result.
    """
    overlaps: list[float] = []
    for before, after in pairs:
        b, a = set(list(before)[:k]), set(list(after)[:k])
        overlaps.append(len(b & a) / len(b) if b else 1.0)
    mean = round(sum(overlaps) / len(overlaps), 4) if overlaps else None
    return {
        "queries": len(pairs),
        "k": k,
        "mean_overlap": mean,
        "min_overlap": round(min(overlaps), 4) if overlaps else None,
        "proves_quality": False,
        "note": (
            "agreement with the incumbent encoding. This cannot prove quality: "
            "matching a reference that was wrong reproduces the error and reads "
            "as a perfect score. Use a labeled query set to make a quality claim."
        ),
        "mode": "agreement",
    }


@dataclass(frozen=True)
class Probe:
    """A query built from the corpus, and the document it should return."""

    query_text: str
    expected: str
    chunk_id: Any


def self_retrieval_probes(
    rows: Sequence[tuple[Any, str, str]], *, fragment_words: int = 12,
) -> list[Probe]:
    """Build a query set from the corpus, with no model and no API key.

    For an ENCODING comparison the question is not "can the model answer" but
    "does the index still return the right chunk". A fragment of a chunk's own
    text, expecting that chunk's document back, tests exactly that — and the
    corpus supplies as many queries as it has chunks.

    A **fragment**, never the whole chunk: a query identical to its document is
    a string match wearing a retrieval test's clothes. Chunks too short to
    fragment are skipped rather than emitted whole.

    Deterministic for a given corpus, because a before and an after must ask the
    identical questions or the comparison measures the question set instead of
    the encoding.

    **What this does and does not show.** Near-neighbour retrieval is the
    easiest case, so this is necessary and not sufficient: degradation here
    proves the encoding lost fidelity, while no degradation shows only that
    near neighbours survived, not that semantic ranking did. Pair it with a
    labeled set for the second claim.
    """
    probes: list[Probe] = []
    for chunk_id, source_path, text in rows:
        words = str(text or "").split()
        # Strictly more words than the fragment, so the query is a proper
        # subset of the document. Equal length would make the query the
        # document, which is a string match wearing a retrieval test's clothes.
        if len(words) <= fragment_words:
            continue
        # The middle, deterministically: the head of a chunk is often a heading
        # or boilerplate that many chunks share.
        start = (len(words) - fragment_words) // 2
        probes.append(Probe(
            query_text=" ".join(words[start:start + fragment_words]),
            expected=str(source_path),
            chunk_id=chunk_id,
        ))
    return probes


__all__ = ["MIN_QUERIES", "Probe", "QueryCase", "agreement", "compare_recall",
           "hit_at_k", "self_retrieval_probes"]
