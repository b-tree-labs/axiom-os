# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Agent incident-interview skill: the canonical questions a reviewer asks +
answers, with live state pulled through an injected query (ADR-074)."""

from __future__ import annotations

from axiom.extensions.builtins.diagnostics.incident_interview import (
    answer,
    suggested_questions,
)

_CTX = {
    "pod": "langfuse/langfuse-clickhouse-shard0-0",
    "reason": "OOMKilled",
    "restarts": 7059,
    "remediation_plan": {"old_limit_bytes": 1536 * 1024**2, "new_limit_bytes": 24 * 1024**3, "reversible": True},
}


def test_suggested_questions_cover_the_reviewer_essentials():
    qs = " ".join(suggested_questions(_CTX)).lower()
    for essential in ("revers", "current", "root", "blast", "do"):
        assert essential in qs


def test_answer_uses_live_state_when_available():
    out = answer("what is the current limit?", _CTX, live=lambda: "Live current limit: 16384 MiB")
    assert out == "Live current limit: 16384 MiB"


def test_answer_falls_back_to_plan_without_live():
    out = answer("current limit?", _CTX)
    assert "1536" in out and "24" in out  # templated from the plan


def test_answer_reversibility_and_rootcause_and_frequency():
    assert "revers" in answer("is this reversible?", _CTX).lower()
    assert "oom" in answer("why is it crash-looping?", _CTX).lower() or "limit" in answer("root cause?", _CTX).lower()
    assert "7059" in answer("how many restarts?", _CTX)


def test_answer_unknown_question_lists_what_it_can_answer():
    out = answer("what's the weather", _CTX).lower()
    assert "current" in out and "revers" in out
