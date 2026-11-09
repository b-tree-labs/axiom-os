# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for the model-coherence gate (axiom-os #499).

The fixture is the field incident that motivated this primitive: a degenerate
served model (``bonsai-1.7b``) emitted incoherent completions and drifted
undetected on a deployment for 68 days because reachability + model-pulled
checks were both green. ``llm.health`` adds the missing quality layer.
"""

from __future__ import annotations

from axiom.llm.health import (
    CoherenceProbe,
    CoherenceReport,
    CoherenceScore,
    check_model_coherence,
    score_coherence,
)

# ---------------------------------------------------------------------------
# score_coherence — the deterministic, model-free degeneracy floor
# ---------------------------------------------------------------------------


def test_coherent_prose_passes():
    s = score_coherence("The capital of France is Paris, a city on the Seine.")
    assert isinstance(s, CoherenceScore)
    assert s.coherent
    assert not s.reasons


def test_empty_is_incoherent():
    assert not score_coherence("").coherent
    assert not score_coherence("   \n  ").coherent


def test_token_loop_is_incoherent():
    # Classic degenerate decode: the same token repeated.
    s = score_coherence("the the the the the the the the the the the the")
    assert not s.coherent
    assert s.repetition_ratio > 0.5
    assert any("repetition" in r for r in s.reasons)


def test_ngram_loop_is_incoherent():
    s = score_coherence("I am I am I am I am I am I am I am I am I am I am")
    assert not s.coherent


def test_low_distinct_ratio_is_incoherent():
    s = score_coherence("yes no yes no yes no yes no yes no yes no yes no yes no")
    assert s.distinct_ratio < 0.35
    assert not s.coherent


def test_gibberish_token_salad_is_incoherent():
    s = score_coherence("asdkfj qweptzxv mnbvcxlkjh zxcvbnmasdf qwertyuiopzxcv lkjhgfdsamnbv")
    assert not s.coherent
    assert any("gibberish" in r for r in s.reasons)


def test_punctuation_storm_is_incoherent():
    s = score_coherence("!!! ??? ... !!! ??? ;;; ::: !!! ??? ... ###")
    assert not s.coherent


def test_short_clean_answer_passes():
    # A terse but valid answer must not be punished for brevity.
    assert score_coherence("Paris").coherent
    assert score_coherence("4").coherent
    assert score_coherence("OK").coherent


# ---------------------------------------------------------------------------
# check_model_coherence — probe battery against a generate() callable
# ---------------------------------------------------------------------------


def _healthy_model(prompt: str) -> str:
    p = prompt.lower()
    if "2 + 2" in p or "2+2" in p:
        return "4"
    if "capital of france" in p:
        return "Paris"
    if "reply with exactly the word" in p:
        return "OK"
    return "I understand the question and here is a coherent answer."


def _degenerate_model(prompt: str) -> str:
    # The bonsai failure mode: looping garbage regardless of prompt.
    return "the the the the the the the the the the the the the the"


def test_healthy_model_passes_gate():
    report = check_model_coherence(_healthy_model)
    assert isinstance(report, CoherenceReport)
    assert report.healthy
    assert report.pass_rate == 1.0
    assert all(p.coherent for p in report.probes)


def test_degenerate_model_fails_gate():
    report = check_model_coherence(_degenerate_model)
    assert not report.healthy
    assert report.pass_rate < 0.66
    assert not any(p.coherent for p in report.probes)


def test_wrong_but_coherent_answer_fails_known_answer_probe():
    # Coherent text that gets the known answer wrong must not pass the probe.
    def confidently_wrong(prompt: str) -> str:
        return "The answer to that question is most certainly five hundred."

    report = check_model_coherence(confidently_wrong)
    # Each probe is coherent prose but fails its must_contain check.
    assert not report.healthy
    assert all(p.coherent for p in report.probes)
    assert not any(p.answered for p in report.probes)


def test_gate_is_tolerant_of_generate_errors():
    # A raising backend must yield a well-formed unhealthy report, never raise
    # (mirrors routing_health's tolerance guarantee for operator surfaces).
    def broken(prompt: str) -> str:
        raise RuntimeError("connection refused")

    report = check_model_coherence(broken)
    assert not report.healthy
    assert all(p.error for p in report.probes)


def test_custom_probes_respected():
    probes = (CoherenceProbe(id="echo", prompt="Say hi", must_contain=("hi",)),)
    report = check_model_coherence(lambda _p: "hi there", probes=probes)
    assert report.healthy
    assert len(report.probes) == 1
    assert report.probes[0].answered
