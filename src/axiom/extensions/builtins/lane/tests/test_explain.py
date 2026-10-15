# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Interpretation: optional, local, and never a precondition.

The rule these all circle is that the floor may not depend on the model.
`doctor` runs in CI and on machines with nothing loaded, so every path here
has to end with the findings still printed.
"""

from __future__ import annotations

from axiom.extensions.builtins.lane import explain
from axiom.extensions.builtins.lane.doctor import BROKEN, DRIFT, Finding

FINDINGS = [
    Finding(BROKEN, ".venv:axiom_appkit", "editable install points at /gone, which does not exist"),
    Finding(DRIFT, ":8790", "python pid 1 is listening and no lane claims it"),
]


def _says(text):
    return lambda system, user: text


def _raises(exc):
    def _ask(system, user):
        raise exc

    return _ask


# --- the floor holds --------------------------------------------------------


def test_findings_print_even_when_no_model_was_asked():
    out = explain.render(FINDINGS, explain.explain(FINDINGS, ask=None))

    assert "axiom_appkit" in out and ":8790" in out


def test_findings_print_even_when_the_model_is_unreachable():
    c = explain.explain(FINDINGS, ask=_raises(FileNotFoundError("no ollama")))
    out = explain.render(FINDINGS, c)

    assert c.unavailable == explain.UNREACHABLE
    assert "axiom_appkit" in out
    assert "stand on their own" in out


def test_an_absent_model_names_itself_rather_than_going_quiet():
    out = explain.render(
        FINDINGS, explain.explain(FINDINGS, ask=_raises(RuntimeError("model xyz not found")))
    )

    assert "ollama pull" in out


def test_not_asking_is_reported_differently_from_being_unable():
    assert explain.explain(FINDINGS, ask=None).unavailable == explain.NOT_REQUESTED
    assert (
        explain.explain(FINDINGS, ask=_raises(FileNotFoundError())).unavailable
        == explain.UNREACHABLE
    )


def test_an_empty_answer_counts_as_no_answer():
    c = explain.explain(FINDINGS, ask=_says("   "))

    assert not c.ok


def test_commentary_never_replaces_the_findings():
    out = explain.render(FINDINGS, explain.explain(FINDINGS, ask=_says("just restart it")))

    assert "axiom_appkit" in out, "the findings must survive the commentary"
    assert "just restart it" in out


def test_a_broken_finding_is_called_out_regardless_of_commentary():
    for c in (explain.explain(FINDINGS, ask=None), explain.explain(FINDINGS, ask=_says("hi"))):
        assert "wrong now" in explain.render(FINDINGS, c)


def test_no_findings_needs_no_model():
    c = explain.explain([], ask=_says("should not be called"))

    assert "Nothing to explain" in c.text


# --- caller_goal (ADR-139) --------------------------------------------------


def test_the_callers_goal_reaches_the_prompt():
    prompt = explain.as_prompt(FINDINGS, caller_goal="start the chat app")

    assert "start the chat app" in prompt
    assert prompt.index("start the chat app") < prompt.index("FINDINGS:"), "goal leads"


def test_the_goal_is_optional_and_absent_leaves_no_trace():
    prompt = explain.as_prompt(FINDINGS)

    assert "CALLER" not in prompt
    assert prompt.startswith("FINDINGS:")


def test_a_blank_goal_is_the_same_as_none():
    assert explain.as_prompt(FINDINGS, caller_goal="   ") == explain.as_prompt(FINDINGS)


def test_the_goal_does_not_change_which_findings_are_reported():
    """It may reorder emphasis in prose. It may not hide a finding."""
    with_goal = explain.render(
        FINDINGS, explain.explain(FINDINGS, ask=None, caller_goal="start chat")
    )
    without = explain.render(FINDINGS, explain.explain(FINDINGS, ask=None))

    for f in FINDINGS:
        assert f.subject in with_goal and f.subject in without


def test_the_system_prompt_tells_the_model_to_answer_for_the_goal():
    assert "THAT goal" in explain.SYSTEM


def test_context_is_included_so_an_inference_has_something_to_stand_on():
    prompt = explain.as_prompt(FINDINGS, context={"worktrees": "a\nb"}, caller_goal="x")

    assert "WORKTREES:" in prompt and "a\nb" in prompt


# --- the house defaults -----------------------------------------------------


def test_it_uses_the_same_local_endpoint_as_the_rest_of_the_platform():
    """A second way to reach the same daemon is how two components disagree
    about whether a model is available."""
    assert explain.OLLAMA_BASE_SETTING == "routing.ollama_base"


def test_quick_and_reasoning_are_separate_roles_with_separate_models():
    """One model is wrong for one of the jobs. A 1B model asked to join three
    facts writes a fluent sentence about one of them."""
    assert explain.DEFAULT_QUICK_MODEL == "qwen2.5:1.5b"
    assert explain.DEFAULT_REASONING_MODEL == "qwen2.5:7b"
    assert explain.QUICK_MODEL_SETTING != explain.REASONING_MODEL_SETTING


def test_both_defaults_are_one_apache_licensed_family():
    """One family is one set of prompt quirks and one licence to track.
    qwen2.5:3b is deliberately absent: it is the only Qwen 2.5 under the
    Research licence, which is more restrictive than Apache-2.0."""
    for model in (explain.DEFAULT_QUICK_MODEL, explain.DEFAULT_REASONING_MODEL):
        assert model.startswith("qwen2.5:")
        assert model != "qwen2.5:3b"


def test_explaining_is_a_reasoning_job_not_a_quick_one():
    assert explain.DEFAULT_MODEL == explain.DEFAULT_REASONING_MODEL
    assert explain.OLLAMA_MODEL_SETTING == explain.REASONING_MODEL_SETTING


def test_its_answer_budget_is_larger_than_the_chat_advisors_and_still_bounded():
    assert 120 < explain.MAX_CHARS <= 2000


# --- fixes are offered, never invented --------------------------------------
#
# Measured 2026-09-28: gemma2:2b, phi3.5:3.8b and qwen2.5:7b ALL invented a
# final command when the prompt asked for "the next command to run" without
# supplying any. Confabulating a plausible command is a property of the tier,
# so the prompt must offer real ones to choose between.


def test_a_findings_fix_travels_with_it_into_the_prompt():
    f = Finding(BROKEN, "x", "broke", fix="pip install -e /real/path")
    prompt = explain.as_prompt([f])

    assert "OFFERED FIX: pip install -e /real/path" in prompt


def test_a_finding_with_no_fix_offers_none():
    prompt = explain.as_prompt([Finding(BROKEN, "x", "broke")])

    assert "OFFERED FIX" not in prompt


def test_the_prompt_forbids_composing_a_command():
    # Whitespace-normalised: the assertion must not depend on where the
    # prose happens to wrap.
    flat = " ".join(explain.SYSTEM.split())

    assert "Never invent" in flat
    assert "do NOT compose a command of your own" in flat


def test_the_prompt_asks_the_model_to_quote_a_fix_verbatim():
    assert "Quote it exactly as given" in " ".join(explain.SYSTEM.split())
