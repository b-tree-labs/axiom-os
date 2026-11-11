# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the NATS-shape subject matcher.

Grammar (per spec-event-bus.md §5):
    subject       := token ('.' token)*
    pattern       := pattern-token ('.' pattern-token)* | pattern-prefix '.>'
    pattern-token := token | '*'
    token         := [a-z0-9_]+

Matching rules:
- `*` matches exactly one token between dots.
- `>` matches one or more tokens; only legal as the final element.
- Subjects/patterns are case-sensitive ASCII lowercase letters, digits, underscores.
"""

from __future__ import annotations

import pytest

from axiom.infra.bus.subjects import InvalidSubjectError, subject_matches, validate_pattern


class TestSubjectMatchesConcrete:
    """Concrete subject-to-concrete-pattern matching."""

    def test_exact_match(self):
        assert subject_matches("tool.post_invoke", "tool.post_invoke")

    def test_exact_mismatch(self):
        assert not subject_matches("tool.post_invoke", "tool.pre_invoke")

    def test_token_count_mismatch(self):
        assert not subject_matches("tool.post_invoke", "tool.post_invoke.extra")
        assert not subject_matches("tool.post_invoke", "tool")


class TestStarWildcard:
    """`*` matches exactly one token."""

    def test_star_matches_one_token(self):
        assert subject_matches("tool.post_invoke", "tool.*")

    def test_star_does_not_match_dots(self):
        # NATS semantics — `*` does not match across dots.
        assert not subject_matches("tool.post.invoke", "tool.*")

    def test_star_in_middle(self):
        assert subject_matches("session.user.ended", "session.*.ended")

    def test_star_at_start(self):
        assert subject_matches("session.ended", "*.ended")
        assert subject_matches("cohort.ended", "*.ended")

    def test_star_with_no_match(self):
        assert not subject_matches("session.user.ended", "*.ended")


class TestGreaterThanWildcard:
    """`>` matches one or more tokens (tail-only)."""

    def test_gt_matches_single_token(self):
        assert subject_matches("tool.post_invoke", "tool.>")

    def test_gt_matches_multiple_tokens(self):
        assert subject_matches("tool.classroom.quiz_submitted", "tool.>")

    def test_gt_requires_at_least_one(self):
        # `>` must match one or more; `tool.>` does not match `tool` alone.
        assert not subject_matches("tool", "tool.>")

    def test_gt_alone_matches_anything(self):
        assert subject_matches("anything", ">")
        assert subject_matches("a.b.c.d.e", ">")

    def test_combined_with_star(self):
        assert subject_matches("tool.foo.bar.baz", "tool.*.bar.>")


class TestValidatePattern:
    """Reject malformed patterns at subscribe time."""

    def test_valid_concrete(self):
        validate_pattern("tool.post_invoke")

    def test_valid_with_star(self):
        validate_pattern("tool.*")
        validate_pattern("*.ended")
        validate_pattern("a.*.b.*.c")

    def test_valid_with_gt(self):
        validate_pattern("tool.>")
        validate_pattern(">")
        validate_pattern("a.*.b.>")

    def test_rejects_uppercase(self):
        with pytest.raises(InvalidSubjectError):
            validate_pattern("Tool.post_invoke")

    def test_rejects_hyphen(self):
        with pytest.raises(InvalidSubjectError):
            validate_pattern("tool.post-invoke")

    def test_rejects_empty(self):
        with pytest.raises(InvalidSubjectError):
            validate_pattern("")

    def test_rejects_empty_token(self):
        with pytest.raises(InvalidSubjectError):
            validate_pattern("tool..invoke")

    def test_rejects_trailing_dot(self):
        with pytest.raises(InvalidSubjectError):
            validate_pattern("tool.")

    def test_rejects_leading_dot(self):
        with pytest.raises(InvalidSubjectError):
            validate_pattern(".tool")

    def test_rejects_gt_not_at_tail(self):
        with pytest.raises(InvalidSubjectError):
            validate_pattern("tool.>.foo")

    def test_rejects_gt_inside_token(self):
        with pytest.raises(InvalidSubjectError):
            validate_pattern("tool.foo>")

    def test_rejects_star_inside_token(self):
        with pytest.raises(InvalidSubjectError):
            validate_pattern("tool.foo*bar")

    def test_rejects_special_chars(self):
        with pytest.raises(InvalidSubjectError):
            validate_pattern("tool.$internal")
        with pytest.raises(InvalidSubjectError):
            validate_pattern("tool /slash")


class TestValidateConcreteSubject:
    """Subjects passed to publish() must not contain wildcards."""

    def test_subject_matches_rejects_wildcard_in_subject(self):
        # The first arg to subject_matches() is a concrete subject; it must
        # never contain `*` or `>`. Defensive.
        with pytest.raises(InvalidSubjectError):
            subject_matches("tool.*", "tool.post_invoke")
        with pytest.raises(InvalidSubjectError):
            subject_matches("tool.>", "tool.post_invoke")
