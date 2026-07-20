# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the file-backed coordinator cohort store.

An instructor's cohort roster must survive process restarts: `axi
classroom invite` (short-lived) registers invites, `axi classroom
serve` (long-running) adds members as students join. Without
persistence, the instructor loses their roster every time they
restart the server.

The store also remembers the coordinator's public URL per classroom
so `invite` can embed it without making the instructor type it twice.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.classroom.classroom_federation import (
    add_member,
    create_cohort,
)
from axiom.extensions.builtins.classroom.coordinator_cohort_store import (
    CohortNotFoundError,
    FileCohortStore,
)

# ---------------------------------------------------------------------------
# Save / load roundtrip
# ---------------------------------------------------------------------------


class TestSaveLoad:
    def test_save_then_load_roundtrips_cohort(self, tmp_path):
        store = FileCohortStore(tmp_path)
        cohort = create_cohort("NE101", "coord_node_abc")
        store.save(cohort)

        loaded = store.load("NE101")
        assert loaded.classroom_id == "NE101"
        assert loaded.coordinator_node == "coord_node_abc"
        assert loaded.members == []

    def test_save_then_load_preserves_members(self, tmp_path):
        store = FileCohortStore(tmp_path)
        cohort = create_cohort("NE101", "coord")
        cohort = add_member(cohort, "alice", "alice_node", "tok_a")
        cohort = add_member(cohort, "bob", "bob_node", "tok_b")
        store.save(cohort)

        loaded = store.load("NE101")
        student_ids = {m.student_id for m in loaded.members}
        assert student_ids == {"alice", "bob"}

    def test_save_then_load_preserves_member_fields(self, tmp_path):
        store = FileCohortStore(tmp_path)
        cohort = create_cohort("NE101", "coord")
        cohort = add_member(cohort, "alice", "alice_node", "tok_a")
        store.save(cohort)

        loaded = store.load("NE101")
        alice = loaded.members[0]
        assert alice.student_id == "alice"
        assert alice.member_node == "alice_node"
        assert alice.invite_token == "tok_a"
        assert alice.status == "ACTIVE"
        assert alice.joined_at is not None

    def test_save_overwrites_prior_state(self, tmp_path):
        store = FileCohortStore(tmp_path)
        c1 = create_cohort("NE101", "coord")
        store.save(c1)
        c2 = add_member(c1, "alice", "n_a", "t_a")
        store.save(c2)

        loaded = store.load("NE101")
        assert len(loaded.members) == 1


# ---------------------------------------------------------------------------
# Coordinator URL (proactive-UX: instructor doesn't re-type it)
# ---------------------------------------------------------------------------


class TestCoordinatorUrl:
    def test_coordinator_url_defaults_to_none(self, tmp_path):
        store = FileCohortStore(tmp_path)
        cohort = create_cohort("NE101", "coord")
        store.save(cohort)

        assert store.get_coordinator_url("NE101") is None

    def test_save_records_coordinator_url(self, tmp_path):
        store = FileCohortStore(tmp_path)
        cohort = create_cohort("NE101", "coord")
        store.save(cohort, coordinator_url="https://test-coordinator.example/classroom/join")

        assert store.get_coordinator_url("NE101") == (
            "https://test-coordinator.example/classroom/join"
        )

    def test_coordinator_url_persists_across_instances(self, tmp_path):
        s1 = FileCohortStore(tmp_path)
        cohort = create_cohort("NE101", "coord")
        s1.save(cohort, coordinator_url="https://x.y/classroom/join")

        s2 = FileCohortStore(tmp_path)
        assert s2.get_coordinator_url("NE101") == "https://x.y/classroom/join"

    def test_save_without_url_preserves_prior_url(self, tmp_path):
        """Once set, a later no-url save doesn't wipe it — invite flow writes
        the URL once, subsequent join flow updates members without URL."""
        store = FileCohortStore(tmp_path)
        cohort = create_cohort("NE101", "coord")
        store.save(cohort, coordinator_url="https://x/classroom/join")

        cohort2 = add_member(cohort, "alice", "n", "t")
        store.save(cohort2)  # no url passed

        assert store.get_coordinator_url("NE101") == "https://x/classroom/join"

    def test_get_url_for_unknown_classroom_raises(self, tmp_path):
        store = FileCohortStore(tmp_path)
        with pytest.raises(CohortNotFoundError):
            store.get_coordinator_url("NE999")


# ---------------------------------------------------------------------------
# Missing / not-found cases
# ---------------------------------------------------------------------------


class TestMissing:
    def test_load_missing_classroom_raises_not_found(self, tmp_path):
        store = FileCohortStore(tmp_path)
        with pytest.raises(CohortNotFoundError):
            store.load("NE999")

    def test_exists_false_when_missing(self, tmp_path):
        store = FileCohortStore(tmp_path)
        assert store.exists("NE999") is False

    def test_exists_true_after_save(self, tmp_path):
        store = FileCohortStore(tmp_path)
        store.save(create_cohort("NE101", "coord"))
        assert store.exists("NE101") is True


# ---------------------------------------------------------------------------
# Listing — for instructor-facing dashboards later
# ---------------------------------------------------------------------------


class TestListing:
    def test_list_ids_empty(self, tmp_path):
        store = FileCohortStore(tmp_path)
        assert store.list_ids() == []

    def test_list_ids_returns_saved_classrooms(self, tmp_path):
        store = FileCohortStore(tmp_path)
        store.save(create_cohort("NE101", "coord"))
        store.save(create_cohort("NE102", "coord"))
        store.save(create_cohort("CS180", "coord"))

        assert store.list_ids() == ["CS180", "NE101", "NE102"]


# ---------------------------------------------------------------------------
# Disk layout + format
# ---------------------------------------------------------------------------


class TestDiskLayout:
    def test_file_is_human_readable_json_per_classroom(self, tmp_path):
        """Instructor should be able to inspect a cohort file by eyeballing it."""
        store = FileCohortStore(tmp_path)
        cohort = create_cohort("NE101", "coord_abc")
        cohort = add_member(cohort, "alice", "alice_node", "tok_a")
        store.save(cohort, coordinator_url="https://x/classroom/join")

        path = tmp_path / "classrooms" / "NE101" / "cohort.json"
        assert path.is_file()
        data = json.loads(path.read_text())
        assert data["cohort"]["classroom_id"] == "NE101"
        assert data["cohort"]["coordinator_node"] == "coord_abc"
        assert data["coordinator_url"] == "https://x/classroom/join"
        assert len(data["cohort"]["members"]) == 1


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------


class TestResilience:
    def test_garbled_file_raises_clear_error(self, tmp_path):
        store = FileCohortStore(tmp_path)
        cohort_path = tmp_path / "classrooms" / "NE101" / "cohort.json"
        cohort_path.parent.mkdir(parents=True)
        cohort_path.write_text("not json {{{")
        with pytest.raises(ValueError, match="corrupt"):
            store.load("NE101")
