# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Grounding metrics for retrieval-augmented answering.

The names follow the published domain literature on retrieval-augmented
assistance in safety-critical engineering — context precision, citation
precision, citation hit, hallucination rate, retrieval recall — deliberately,
so a number produced here can be set beside a published one instead of living
in a private dialect nobody can check.

**The source work annotates these by hand.** It tried automatic schemes —
citation-string matching, embedding similarity — found them brittle, and
replaced them with expert-verified definitions. That is the rigorous answer, and
it is also why that benchmark is 27 queries: expert annotation does not scale to
running on every change.

What this module offers is a different trade, not a better instrument. The
output-provenance gate already decides, per answer and deterministically,
whether every value stated is supported by that turn's evidence, so the
grounding terms cost nothing per item and can run continuously. An expert can
tell whether a claim is TRUE; the gate can only tell whether a stated value is
SUPPORTED by the evidence present. The two are complements — the computed rate
catches regressions between annotations, and annotation calibrates what the
computed rate is worth.

Known divergences and undetermined parameters are catalogued in
``docs/working/grounding-metrics-open-questions-2026-09-21.md``. Read it before
placing a number from here beside a published one — our claim population is
narrower, and several parameter choices were ours to make.

**Two of these cannot be computed at all, and say so.** The semantic half of
context precision is an expert grade on a discrete scale, and citation validity
depends on whether a source substantively supports a claim. Both are accepted as
inputs here rather than estimated, because estimating them would produce a
number that looks like the published one and is not.

The gate also separates two failures a single rate would blur: a value that the
evidence does not support, and a claim that a tool produced a value when no tool
ran. The second can carry a *correct* number and still mislead, because what a
reader relies on is the claim about where it came from.

Visual recall in the source work is generalised here to retrieval recall. A
figure is one kind of thing worth retrieving; a base metric should not privilege
one modality, and a consumer that cares specifically about figures can pass the
figure set as the relevant one.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

#: Published short names, kept so scores stay comparable with the literature.
METRIC_ALIASES: dict[str, str] = {
    "context_precision": "CoP",
    "citation_precision": "CiP",
    "citation_hit": "CiH",
    "hallucination_rate_claims": "HR",
    "retrieval_recall": "ViR",
}


@dataclass(frozen=True)
class GroundingScores:
    """All five together.

    Reported as a bundle on purpose: a good citation score can hide a bad
    grounding one, and a benchmark exists to make that trade visible rather than
    average it away.
    """

    context_precision: float = 0.0
    citation_precision: float = 0.0
    citation_hit: float = 0.0
    hallucination_rate: float = 0.0
    retrieval_recall: float = 0.0

    def as_dict(self) -> dict[str, float]:
        """Keyed by the published short names."""
        return {
            METRIC_ALIASES["context_precision"]: self.context_precision,
            METRIC_ALIASES["citation_precision"]: self.citation_precision,
            METRIC_ALIASES["citation_hit"]: self.citation_hit,
            METRIC_ALIASES["hallucination_rate_claims"]: self.hallucination_rate,
            METRIC_ALIASES["retrieval_recall"]: self.retrieval_recall,
        }


def _ratio(numerator: int, denominator: int, *, empty: float = 0.0) -> float:
    return round(numerator / denominator, 6) if denominator else empty


#: The discrete grades the published semantic term uses. An off-scale value is
#: refused rather than accepted, because a number that silently leaves the scale
#: is incomparable while still looking like a score.
SEMANTIC_GRADES: frozenset[float] = frozenset({0.0, 0.25, 0.5, 0.75, 1.0})

#: Weight on the semantic term for mixed queries, per the source work.
DEFAULT_ALPHA = 0.6


def numeric_accuracy(*, predicted: Sequence[float], reference: Sequence[float],
                     epsilon: float = 1e-9) -> float:
    """Mean of ``1 - |v - v*| / max(|v*|, eps)`` over paired values.

    Floored at zero per value. The expression is unbounded below, so one
    catastrophic number would otherwise drag a run's mean negative and make the
    metric unreadable rather than merely bad.

    No values to compare scores 1.0: there was nothing to get wrong, and
    penalising a question that asked for none would measure the question.
    """
    pairs = list(zip(predicted, reference))
    if not pairs:
        return 1.0
    total = 0.0
    for value, expected in pairs:
        denominator = max(abs(expected), epsilon)
        total += max(0.0, 1.0 - abs(value - expected) / denominator)
    return round(total / len(pairs), 6)


def context_precision(
    *,
    semantic: float,
    predicted: Sequence[float] = (),
    reference: Sequence[float] = (),
    alpha: float = DEFAULT_ALPHA,
) -> float:
    """Blended answer correctness: ``alpha * semantic + (1 - alpha) * numeric``.

    Despite the name, this is NOT precision over retrieved context — it is the
    source work's correctness score, and an earlier implementation here measured
    retrieval precision instead, which is a different quantity sharing a name.

    The semantic term is an EXPERT grade on a discrete scale. This module does
    not compute it and does not pretend to; the numeric term it can compute
    exactly, which is the half worth automating.
    """
    if semantic not in SEMANTIC_GRADES:
        raise ValueError(
            f"semantic grade {semantic} is off the published scale "
            f"{sorted(SEMANTIC_GRADES)}; a score off the scale cannot be compared"
        )
    if not list(zip(predicted, reference)):
        # The published alpha of 0.6 is specified "for mixed queries". A query
        # with no numeric values is not mixed, so blending in a vacuous numeric
        # 1.0 would inflate it — a purely qualitative answer graded 0.75 would
        # report 0.85. With nothing numeric to weigh, the grade IS the score.
        return round(semantic, 6)
    return round(
        alpha * semantic + (1.0 - alpha) * numeric_accuracy(
            predicted=predicted, reference=reference
        ),
        6,
    )


def retrieval_precision(*, retrieved: Sequence[Any], relevant: Collection[Any]) -> float:
    """Of what was retrieved, how much was worth retrieving.

    A real and useful quantity — it simply is not what the published context
    precision measures, so it does not carry that name here. Conflating the two
    is how a comparison table ends up putting unlike numbers in one column.
    """
    relevant_set = set(relevant)
    return _ratio(sum(1 for item in retrieved if item in relevant_set), len(retrieved))


def citation_precision(*, cited: Sequence[Any], valid: Collection[Any]) -> float:
    """Of the citations offered, how many actually support the answer.

    An answer citing four sources and getting two right is not fully cited, and
    treating it as such is how a citation count becomes decoration.
    """
    valid_set = set(valid)
    return _ratio(sum(1 for item in cited if item in valid_set), len(cited))


def citation_hit(*, cited: Sequence[Any], valid: Collection[Any]) -> float:
    """Whether ANY offered citation landed. Coarser than precision by design:
    it answers "was this answer attributable at all", which is a different
    question from "was it attributed well"."""
    valid_set = set(valid)
    return 1.0 if any(item in valid_set for item in cited) else 0.0


def retrieval_recall(*, retrieved: Sequence[Any], relevant: Collection[Any]) -> float:
    """Of what mattered, how much was found.

    An empty relevant set scores 1.0: nothing needed finding, so nothing was
    missed. Scoring it zero would punish a question that had no retrievable
    ground truth, which is a property of the question, not the assistant.
    """
    relevant_set = set(relevant)
    if not relevant_set:
        return 1.0
    found = sum(1 for item in relevant_set if item in set(retrieved))
    return _ratio(found, len(relevant_set), empty=1.0)


def ungrounded_answer_rate(
    answers: Iterable[tuple[str, Sequence[str]]],
    *,
    evidence: Sequence[str],
    config: Any = None,
) -> float:
    """Fraction of ANSWERS stating something their evidence does not support.

    Note the granularity. The published hallucination rate is claim-level —
    unsupported claims over total claims — so this is a different number and
    must not be reported beside one. It is kept because it is what the gate
    yields directly and it gates a release well: any ungrounded answer is a
    failure regardless of how many claims it contained.

    Computed by the output-provenance gate rather than estimated by a judge.
    Each answer is paired with the tools called on that turn, because claiming a
    tool produced a value when none ran is its own failure — the number may be
    right and the attribution still invented.

    An abstention is NOT a hallucination. Declining to state a value that cannot
    be grounded is the behaviour the gate exists to produce, and counting it here
    would turn the metric into an argument for switching the gate off.
    """
    from axiom.rag.provenance import ProvenanceGateConfig, evaluate_answer_provenance

    gate_config = config or ProvenanceGateConfig()
    total = 0
    hallucinated = 0
    for answer, tools in answers:
        total += 1
        decision = evaluate_answer_provenance(
            answer,
            grounded_texts=evidence,
            tools_called=tuple(tools),
            config=gate_config,
        )
        if not decision.grounded:
            hallucinated += 1
    return _ratio(hallucinated, total)


def hallucination_rate_claims(*, unsupported: int, total: int) -> float:
    """``|unsupported claims| / |total claims|`` — the published granularity.

    Claim-level rather than answer-level because an answer with one bad value
    among ten is not as wrong as an answer with ten, and answer-level accounting
    cannot tell those apart. Our gate reports the specific unsupported values it
    found, so the numerator is available; the denominator is the count of claims
    the gate considered, which the caller supplies.
    """
    return _ratio(unsupported, total)
