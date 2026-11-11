# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The gate a precision or dimensionality change has to clear.

Halving the width of an embedding, or truncating it, frees a great deal of disk.
It also changes what comes back. A storage win that quietly costs recall is a
regression, and disk is much easier to measure than retrieval quality, so the
temptation is to report the easy number and call it a result.

Two modes, and the difference between them is the whole point:

**Labeled.** A query set where the expected document is known. Retrieval either
returns it in the top k or does not, for each arm, over the same queries. That
is paired binary data, so McNemar applies — the same test the comparative
battery uses. This mode can prove quality held.

**Agreement.** No labels; the current encoding's results are the reference. It
measures how far the new results drift from the old. This can detect a change
and can never prove quality, because agreeing with the incumbent is not the same
as being right. It says so, and the milestone requires labeled.
"""

from __future__ import annotations

from axiom.rag.recall_compare import (
    QueryCase,
    agreement,
    compare_recall,
    hit_at_k,
)


def _case(qid, expected, before, after):
    return QueryCase(query_id=qid, expected=expected, before=before, after=after)


def test_hit_at_k_is_membership_in_the_top_k():
    assert hit_at_k(["a", "b", "c"], expected="b", k=3) is True
    assert hit_at_k(["a", "b", "c"], expected="b", k=1) is False
    assert hit_at_k([], expected="b", k=5) is False


def test_identical_arms_show_no_significant_change():
    """The negative control. If this ever reports a difference between an
    encoding and itself, every later result it produces is noise."""
    cases = [_case(i, "x", ["x", "y"], ["x", "y"]) for i in range(30)]
    result = compare_recall(cases, k=1)
    assert result["discordant"] == 0
    assert result["significant"] is False
    assert result["regression"] is False


def test_a_real_degradation_is_caught_and_named_a_regression():
    """The case the gate exists for: storage fell and retrieval got worse."""
    cases = [_case(i, "x", ["x"], ["y"]) for i in range(15)] + \
            [_case(i + 100, "x", ["x"], ["x"]) for i in range(15)]
    result = compare_recall(cases, k=1)
    assert result["significant"] is True
    assert result["regression"] is True
    assert result["hit_rate_before"] > result["hit_rate_after"]


def test_an_improvement_is_not_called_a_regression():
    cases = [_case(i, "x", ["y"], ["x"]) for i in range(15)] + \
            [_case(i + 100, "x", ["x"], ["x"]) for i in range(15)]
    result = compare_recall(cases, k=1)
    assert result["significant"] is True
    assert result["regression"] is False


def test_a_small_query_set_cannot_clear_the_gate():
    """Four queries that all agree is not evidence that recall held; it is an
    absence of evidence. A gate that passes on it would pass on anything."""
    cases = [_case(i, "x", ["x"], ["x"]) for i in range(4)]
    result = compare_recall(cases, k=1, min_queries=30)
    assert result["passes_gate"] is False
    assert "too few" in result["note"].lower()


def test_an_adequate_set_with_no_change_clears_the_gate():
    """The positive control. A gate that never passes is not a gate."""
    cases = [_case(i, "x", ["x"], ["x"]) for i in range(40)]
    result = compare_recall(cases, k=1, min_queries=30)
    assert result["passes_gate"] is True


def test_a_significant_regression_fails_the_gate_however_large_the_set():
    cases = [_case(i, "x", ["x"], ["y"]) for i in range(20)] + \
            [_case(i + 100, "x", ["x"], ["x"]) for i in range(40)]
    result = compare_recall(cases, k=1, min_queries=30)
    assert result["passes_gate"] is False


def test_agreement_mode_reports_overlap_and_refuses_to_claim_quality():
    """Without labels, matching the incumbent is all that can be measured, and
    that is not the same as being right."""
    result = agreement([
        (["a", "b", "c"], ["a", "b", "c"]),
        (["a", "b", "c"], ["a", "b", "z"]),
    ], k=3)
    assert result["mean_overlap"] == 0.83 or abs(result["mean_overlap"] - 5 / 6) < 0.01
    assert result["proves_quality"] is False
    assert "cannot prove" in result["note"].lower()


def test_agreement_on_identical_results_is_total():
    result = agreement([(["a", "b"], ["a", "b"])], k=2)
    assert result["mean_overlap"] == 1.0
    assert result["proves_quality"] is False


def test_the_report_carries_the_counts_behind_the_verdict():
    """A boolean invites quoting the verdict and dropping the evidence. The
    discordant count is what says whether a p-value rests on four queries or
    four hundred."""
    cases = [_case(i, "x", ["x"], ["y"]) for i in range(10)] + \
            [_case(i + 100, "x", ["x"], ["x"]) for i in range(30)]
    result = compare_recall(cases, k=1)
    for key in ("queries", "discordant", "lost", "gained",
                "hit_rate_before", "hit_rate_after", "p_value"):
        assert key in result, key
    assert result["lost"] == 10 and result["gained"] == 0


# --- building the query set without an LLM ------------------------------------


def test_self_retrieval_probes_come_from_the_corpus_itself():
    """A cheaper and more precise query set for an ENCODING comparison.

    The question being asked is not "can the model answer" but "does the index
    still return the right chunk". A fragment of a chunk's own text, expecting
    that chunk back, tests exactly that — with no model, no API key, and as many
    queries as the corpus has chunks.
    """
    from axiom.rag.recall_compare import self_retrieval_probes

    rows = [
        (1, "/docs/a.md", "the reactor scrammed at 14:32 following a rod drift alarm"),
        (2, "/docs/b.md", "coolant flow was restored after the pump restart completed"),
    ]
    probes = self_retrieval_probes(rows, fragment_words=6)
    assert len(probes) == 2
    assert probes[0].expected == "/docs/a.md"
    assert probes[0].query_text and probes[0].query_text in rows[0][2]
    # A fragment, not the whole chunk: an identical string is a trivial lookup.
    assert len(probes[0].query_text.split()) <= 6


def test_a_chunk_too_short_to_fragment_is_skipped():
    """Skipped rather than emitted whole. A probe whose query IS the document is
    a string match wearing a retrieval test's clothes."""
    from axiom.rag.recall_compare import self_retrieval_probes

    rows = [(1, "/a.md", "short"), (2, "/b.md", "one two three four five six seven eight")]
    probes = self_retrieval_probes(rows, fragment_words=6)
    assert [p.expected for p in probes] == ["/b.md"]


def test_probes_are_deterministic_for_a_given_corpus():
    """A before and after must ask the identical questions, or the comparison
    measures the question set instead of the encoding."""
    from axiom.rag.recall_compare import self_retrieval_probes

    rows = [(i, f"/d{i}.md", " ".join(f"w{i}x{j}" for j in range(20))) for i in range(5)]
    assert [p.query_text for p in self_retrieval_probes(rows)] == \
           [p.query_text for p in self_retrieval_probes(rows)]
