# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for course lifecycle state machine + semver.

Per spec-classroom.md §2.6 (Course lifecycle): a Course transitions
through draft → review → published → deprecated. Publishing bumps
the version (semver); subsequent edits to a published course require
a version bump.

Semver: v1.2.3 (major.minor.patch).
- major: breaking changes (e.g., objectives removed)
- minor: new content added (new assessments, new corpus)
- patch: fixes (typo, clarification)
"""

from __future__ import annotations

import pytest


class TestSemverParsing:
    def test_valid_semver(self):
        from axiom.extensions.builtins.classroom.course_lifecycle import (
            parse_semver,
        )

        assert parse_semver("1.2.3") == (1, 2, 3)
        assert parse_semver("0.1.0") == (0, 1, 0)
        assert parse_semver("10.20.30") == (10, 20, 30)

    def test_invalid_semver_raises(self):
        from axiom.extensions.builtins.classroom.course_lifecycle import (
            parse_semver,
        )

        with pytest.raises(ValueError, match="semver"):
            parse_semver("1.2")
        with pytest.raises(ValueError, match="semver"):
            parse_semver("not-a-version")


class TestSemverBump:
    def test_bump_major(self):
        from axiom.extensions.builtins.classroom.course_lifecycle import bump_version

        assert bump_version("1.2.3", "major") == "2.0.0"

    def test_bump_minor(self):
        from axiom.extensions.builtins.classroom.course_lifecycle import bump_version

        assert bump_version("1.2.3", "minor") == "1.3.0"

    def test_bump_patch(self):
        from axiom.extensions.builtins.classroom.course_lifecycle import bump_version

        assert bump_version("1.2.3", "patch") == "1.2.4"

    def test_invalid_bump_type_raises(self):
        from axiom.extensions.builtins.classroom.course_lifecycle import bump_version

        with pytest.raises(ValueError, match="bump"):
            bump_version("1.2.3", "super")


class TestStateMachine:
    def test_initial_status_draft(self):
        from axiom.extensions.builtins.classroom.course_lifecycle import CourseState

        s = CourseState(course_id="c", version="0.1.0")
        assert s.status == "draft"

    def test_draft_to_review(self):
        from axiom.extensions.builtins.classroom.course_lifecycle import (
            CourseState,
            submit_for_review,
        )

        s = CourseState(course_id="c", version="0.1.0")
        s = submit_for_review(s, submitter="ben@ut.edu")
        assert s.status == "review"
        assert s.submitted_by == "ben@ut.edu"

    def test_review_to_published_bumps_to_1_0_0(self):
        from axiom.extensions.builtins.classroom.course_lifecycle import (
            CourseState,
            publish,
            submit_for_review,
        )

        s = CourseState(course_id="c", version="0.1.0")
        s = submit_for_review(s, submitter="ben@ut.edu")
        s = publish(s, approver="reviewer@ut.edu")

        assert s.status == "published"
        assert s.version == "1.0.0"
        assert s.published_by == "reviewer@ut.edu"

    def test_published_to_deprecated(self):
        from axiom.extensions.builtins.classroom.course_lifecycle import (
            CourseState,
            deprecate,
            publish,
            submit_for_review,
        )

        s = CourseState(course_id="c", version="0.1.0")
        s = submit_for_review(s, submitter="i")
        s = publish(s, approver="r")
        s = deprecate(s, reason="superseded by v2")

        assert s.status == "deprecated"
        assert s.deprecation_reason == "superseded by v2"

    def test_invalid_transitions_raise(self):
        from axiom.extensions.builtins.classroom.course_lifecycle import (
            CourseState,
            publish,
        )

        s = CourseState(course_id="c", version="0.1.0")  # draft
        # Can't publish directly from draft — must go through review
        with pytest.raises(ValueError, match="review"):
            publish(s, approver="r")


class TestVersionBumpOnRepublish:
    def test_edit_after_publish_requires_bump(self):
        from axiom.extensions.builtins.classroom.course_lifecycle import (
            CourseState,
            publish,
            republish_with_bump,
            submit_for_review,
        )

        s = CourseState(course_id="c", version="0.1.0")
        s = submit_for_review(s, submitter="i")
        s = publish(s, approver="r")
        assert s.version == "1.0.0"

        # Add new content → minor bump
        s = republish_with_bump(s, bump_type="minor", approver="r",
                                notes="added lecture 5")
        assert s.version == "1.1.0"
        assert s.status == "published"
        assert len(s.version_history) >= 2

    def test_cannot_republish_unpublished(self):
        from axiom.extensions.builtins.classroom.course_lifecycle import (
            CourseState,
            republish_with_bump,
        )

        s = CourseState(course_id="c", version="0.1.0")  # draft
        with pytest.raises(ValueError, match="published"):
            republish_with_bump(s, bump_type="minor", approver="r", notes="x")


class TestVersionHistory:
    def test_history_grows_with_each_publish(self):
        from axiom.extensions.builtins.classroom.course_lifecycle import (
            CourseState,
            publish,
            republish_with_bump,
            submit_for_review,
        )

        s = CourseState(course_id="c", version="0.1.0")
        s = submit_for_review(s, submitter="i")
        s = publish(s, approver="r")
        s = republish_with_bump(s, "patch", approver="r", notes="typo fix")
        s = republish_with_bump(s, "minor", approver="r", notes="new section")

        versions = [h["version"] for h in s.version_history]
        assert versions == ["1.0.0", "1.0.1", "1.1.0"]


class TestArtifactRegistryHook:
    """Integration hook: published courses register as artifacts."""

    def test_publish_emits_registry_event(self):
        from axiom.extensions.builtins.classroom.course_lifecycle import (
            CourseState,
            publish,
            submit_for_review,
        )

        registry_events = []

        def fake_registry(event):
            registry_events.append(event)

        s = CourseState(course_id="c", version="0.1.0")
        s = submit_for_review(s, submitter="i")
        s = publish(s, approver="r", registry=fake_registry)

        assert len(registry_events) == 1
        assert registry_events[0]["type"] == "course_published"
        assert registry_events[0]["course_id"] == "c"
        assert registry_events[0]["version"] == "1.0.0"
