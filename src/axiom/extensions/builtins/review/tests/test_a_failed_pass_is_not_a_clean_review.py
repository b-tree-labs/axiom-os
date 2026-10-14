# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A review pass that could not run must not read as "nothing found".

Every pass returned `[]` for three unrelated situations:

    the model found nothing          <- correct, a clean pass
    the model's answer did not parse <- a lie
    the LLM call failed outright     <- a lie

`run()` wrapped the call in `except Exception: log.warning(...); return []`, so a
network blip, an expired key or a rate limit rendered as a clean security
review. `agent.py` compounded it: a pass that raises is caught, logged and
skipped, so all five passes could fail and the run would still hand back a
FindingSet that reads as "no problems found".

That is a false green in the tool whose entire job is catching problems.

The fix follows a precedent already in this package rather than inventing one:
`gather_context` returns `(ctx, ctx_warnings)` where the warnings are Finding
objects injected into the result, so a degraded run is visible to whoever reads
the review. A pass that cannot complete now emits the same kind of finding.

The parse helper was copy-pasted across all five passes — byte-identical in
four of them — so it is fixed once, in one shared module, rather than five
times.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.review.agents.rev_u.passes import (
    correctness, docs, performance, security, tests as tests_pass,
)
from axiom.extensions.builtins.review.agents.rev_u.passes._parse import (
    PassOutputUnreadable,
    parse_findings,
)

ALL_PASSES = [correctness, docs, performance, security, tests_pass]


class _LLM:
    """Stands in for the model. `text` is returned; `boom` raises instead."""

    def __init__(self, text: str = "[]", boom: Exception | None = None):
        self._text = text
        self._boom = boom

    def complete(self, prompt, system=None):
        if self._boom is not None:
            raise self._boom
        return type("R", (), {"text": self._text})()


# --- the shared parser ------------------------------------------------------

def test_an_empty_list_really_is_no_findings():
    assert parse_findings("[]", pass_kind="security") == []


def test_unparseable_output_is_not_silence():
    with pytest.raises(PassOutputUnreadable):
        parse_findings("I could not review this diff, sorry.", pass_kind="security")


def test_output_of_the_wrong_shape_is_not_silence():
    with pytest.raises(PassOutputUnreadable):
        parse_findings('{"not": "a list"}', pass_kind="security")


def test_a_well_formed_finding_survives():
    out = parse_findings(
        '[{"severity": "major", "path": "a.py", "line": 3, "message": "x"}]',
        pass_kind="security",
    )
    assert len(out) == 1 and out[0].message == "x" and out[0].pass_kind == "security"


# --- every pass, same contract ---------------------------------------------

@pytest.mark.parametrize("mod", ALL_PASSES, ids=lambda m: m.PASS_KIND)
def test_a_pass_that_cannot_reach_the_model_says_so(mod):
    out = mod.run("diff", {}, _LLM(boom=RuntimeError("connection refused")))
    assert out, f"{mod.PASS_KIND} reported a clean pass after the LLM call failed"
    assert any("did not complete" in f.message.lower() for f in out), out


@pytest.mark.parametrize("mod", ALL_PASSES, ids=lambda m: m.PASS_KIND)
def test_a_pass_whose_output_is_unreadable_says_so(mod):
    out = mod.run("diff", {}, _LLM(text="not json at all"))
    assert out, f"{mod.PASS_KIND} reported a clean pass on an unreadable answer"
    assert any("did not complete" in f.message.lower() for f in out), out


@pytest.mark.parametrize("mod", ALL_PASSES, ids=lambda m: m.PASS_KIND)
def test_a_genuinely_clean_pass_stays_clean(mod):
    """The whole point is that a real clean review is still readable as one."""
    assert mod.run("diff", {}, _LLM(text="[]")) == []


# --- the agent level --------------------------------------------------------

def test_the_agent_records_a_pass_that_raised_rather_than_only_logging():
    """The compounding half.

    `agent.review` caught a raising pass, logged it and continued. With all five
    failing, the FindingSet still came back looking like a clean review. The
    surviving passes should still run — that part was right — but the failure
    has to appear in the result someone reads.
    """
    from unittest.mock import patch

    from axiom.extensions.builtins.review.agents.rev_u.agent import RevUAgent

    agent = RevUAgent(llm=_LLM(text="[]"))
    with patch(
        "axiom.extensions.builtins.review.tools.context.gather_context",
        return_value=({}, []),
    ), patch(
        "axiom.extensions.builtins.review.agents.rev_u.passes.correctness.run",
        side_effect=RuntimeError("boom"),
    ):
        fset = agent.review(
            "diff", passes=["correctness", "security"], run_validator=False
        )

    messages = " ".join(f.message.lower() for f in fset.findings)
    assert "did not complete" in messages, (
        "a pass raised and the review still read as clean: "
        f"{[f.message for f in fset.findings]}"
    )


def test_an_unknown_pass_name_is_not_silently_dropped():
    """Asking for a pass that does not exist must not look like it ran clean."""
    from axiom.extensions.builtins.review.agents.rev_u.agent import RevUAgent
    from unittest.mock import patch

    agent = RevUAgent(llm=_LLM(text="[]"))
    with patch(
        "axiom.extensions.builtins.review.tools.context.gather_context",
        return_value=({}, []),
    ):
        fset = agent.review("diff", passes=["no-such-pass"], run_validator=False)

    assert fset.findings, "an unknown pass produced a silently empty review"
