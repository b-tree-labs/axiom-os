# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the conftest pollution-detection helper.

The session-end guard in ``conftest.py`` MUST unset (not restore) a
polluted snapshot. Without this discrimination the conftest itself
becomes the persistence vector for the 2026-05-04 incident's user.name
pollution: snapshot=polluted → end=clean → guard re-installs pollution.
"""

from __future__ import annotations

import pytest

from tests._pollution_guard import (
    KNOWN_POLLUTION_EMAILS,
    KNOWN_POLLUTION_NAMES,
    all_commits_are_pollution,
    is_polluted_snapshot,
)


class TestKnownMarkers:
    def test_test_is_polluted_name(self):
        assert "Test" in KNOWN_POLLUTION_NAMES

    def test_tester_is_polluted_name(self):
        assert "tester" in KNOWN_POLLUTION_NAMES

    def test_global_leak_probe_is_polluted_name(self):
        assert "GLOBAL-LEAK-PROBE" in KNOWN_POLLUTION_NAMES

    def test_test_at_example_is_polluted_email(self):
        assert "test@example.com" in KNOWN_POLLUTION_EMAILS


class TestPollutedSnapshotDetection:
    def test_clean_snapshot_is_not_polluted(self):
        assert is_polluted_snapshot("Benjamin Booth", "ben@b-treeventures.com") is False

    def test_test_name_alone_is_polluted(self):
        assert is_polluted_snapshot("Test", "ben@b-treeventures.com") is True

    def test_test_email_alone_is_polluted(self):
        assert is_polluted_snapshot("Benjamin Booth", "test@example.com") is True

    def test_both_polluted_is_polluted(self):
        assert is_polluted_snapshot("Test", "test@example.com") is True

    def test_none_none_is_not_polluted(self):
        """Empty snapshot means 'no local override' — the clean state.
        Restoring None (unset) is correct, not pollution to detect."""
        assert is_polluted_snapshot(None, None) is False

    def test_none_with_test_email_still_polluted(self):
        assert is_polluted_snapshot(None, "test@example.com") is True

    def test_test_name_with_none_email_still_polluted(self):
        assert is_polluted_snapshot("Test", None) is True


class TestAllCommitsArePollution:
    """Gate for safe session-end HEAD auto-heal: only reset --hard when
    EVERY commit in the moved range is a stray test-fixture author."""

    def test_empty_range_is_not_pollution(self):
        assert all_commits_are_pollution([]) is False

    def test_all_test_authored_is_pollution(self):
        authors = [("Test", "test@example.com"), ("tester", "t@t.test")]
        assert all_commits_are_pollution(authors) is True

    def test_single_real_author_makes_range_unsafe(self):
        authors = [
            ("Test", "test@example.com"),
            ("Benjamin Booth", "ben@b-treeventures.com"),
        ]
        assert all_commits_are_pollution(authors) is False

    def test_one_polluted_field_per_commit_is_pollution(self):
        authors = [("Test", "ben@b-treeventures.com"), (None, "test@example.com")]
        assert all_commits_are_pollution(authors) is True


@pytest.mark.parametrize("pollution_name", sorted(KNOWN_POLLUTION_NAMES))
def test_each_known_pollution_name_detected(pollution_name):
    assert is_polluted_snapshot(pollution_name, None) is True


@pytest.mark.parametrize("pollution_email", sorted(KNOWN_POLLUTION_EMAILS))
def test_each_known_pollution_email_detected(pollution_email):
    assert is_polluted_snapshot(None, pollution_email) is True
