# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The interview is resumable, re-runnable, and safe when nobody is there."""

from __future__ import annotations

from axiom.extensions.builtins.principal.interview import (
    QUESTIONS,
    apply_answers,
    next_question,
)
from axiom.extensions.builtins.principal.models import PrincipalProfile, PrincipalStatus


def test_questions_cover_the_p0_record():
    keys = {q.key for q in QUESTIONS}
    assert {"display_name", "endpoints", "preferences", "quiet_hours"} <= keys


def test_next_question_returns_the_first_unanswered():
    assert next_question({}).key == QUESTIONS[0].key
    answered = {QUESTIONS[0].key: "someone"}
    assert next_question(answered).key == QUESTIONS[1].key


def test_next_question_is_none_when_required_answers_are_present():
    answers = {q.key: "x" for q in QUESTIONS if q.required}
    assert next_question(answers) is None


def test_apply_answers_builds_an_unverified_principal():
    p = apply_answers(
        {
            "display_name": "Test Principal",
            "endpoints": [{"kind": "email", "address": "p@example.test"}],
            "preferences": [{"topic_class": "*", "ranked_kinds": ["email"]}],
        },
        handle="@ben:netl",
    )
    assert isinstance(p, PrincipalProfile)
    assert p.status is PrincipalStatus.UNVERIFIED, "answers alone never make a principal active"
    assert p.endpoints[0].verified_at is None


def test_partial_answers_are_resumable_not_fatal():
    p = apply_answers({"display_name": "Half Done"}, handle="@ben:netl")
    assert p.status is PrincipalStatus.PENDING
    assert next_question({"display_name": "Half Done"}) is not None


def test_handle_is_required_and_must_match_the_platform_grammar():
    """DESK keys on the same @name:context handle the rest of the platform uses.

    Inventing a second identifier scheme would leave this record unable to join to
    PrincipalContext or the federation Principal -- a reachability layer that
    cannot be reached from the identity layer above it.
    """
    import pytest

    with pytest.raises(ValueError, match="handle"):
        apply_answers({"display_name": "x"}, handle="")
    with pytest.raises(ValueError, match="@"):
        apply_answers({"display_name": "x"}, handle="ben@example.test")
    with pytest.raises(ValueError, match="fediverse|handle"):
        apply_answers({"display_name": "x"}, handle="@ben@example.test")


def test_directory_ref_is_an_attribute_not_the_key():
    """The ADR-103 directory id is carried, but the handle is what joins."""
    p = apply_answers({"display_name": "x"}, handle="@ben:netl", directory_ref="oid:abc123")
    assert p.handle == "@ben:netl"
    assert p.directory_ref == "oid:abc123"


def test_handle_context_is_available_for_routing_decisions():
    p = apply_answers({"display_name": "x"}, handle="@ben:netl")
    assert p.name == "ben"
    assert p.context == "netl"
