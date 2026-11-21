# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for federated course pack distribution mid-course (§5.9).

Per spec-classroom.md §5.9. Flow:
1. Instructor publishes updated .axiompack (via course_lifecycle
   republish_with_bump).
2. Distribution state records pending update for each active member.
3. Students accept (opt-in) or keep pinned version. Quarantined
   members excluded automatically.
4. Turn traces carry pack_version so replays stay accurate across
   mid-course updates.
"""

from __future__ import annotations

import pytest


def _cohort_with_members(*student_ids):
    from axiom.extensions.builtins.classroom.classroom_federation import (
        add_member,
        create_cohort,
    )

    c = create_cohort("cr", "hub")
    for sid in student_ids:
        c = add_member(c, sid, f"{sid}-node", f"tok-{sid}")
    return c


class TestInitialPin:
    def test_all_members_pinned_to_initial_version(self):
        from axiom.extensions.builtins.classroom.pack_distribution import (
            init_distribution,
        )

        cohort = _cohort_with_members("s1", "s2", "s3")
        dist = init_distribution(cohort=cohort, initial_version="1.0.0")

        assert dist.classroom_id == "cr"
        assert dist.active_version == "1.0.0"
        assert dist.pinned_versions == {
            "s1": "1.0.0", "s2": "1.0.0", "s3": "1.0.0"
        }
        assert dist.pending_updates == {}


class TestPublishUpdate:
    def test_publish_creates_pending_updates_for_active_members(self):
        from axiom.extensions.builtins.classroom.classroom_federation import (
            quarantine_member,
        )
        from axiom.extensions.builtins.classroom.pack_distribution import (
            init_distribution,
            publish_update,
        )

        cohort = _cohort_with_members("s1", "s2", "s3")
        cohort = quarantine_member(cohort, "s3", reason="trust break")
        dist = init_distribution(cohort=cohort, initial_version="1.0.0")

        dist = publish_update(dist, cohort=cohort, new_version="1.1.0",
                              notes="added lecture 5")

        assert dist.active_version == "1.1.0"
        assert set(dist.pending_updates.keys()) == {"s1", "s2"}
        # s3 quarantined — excluded
        assert "s3" not in dist.pending_updates

    def test_downgrade_rejected(self):
        from axiom.extensions.builtins.classroom.pack_distribution import (
            init_distribution,
            publish_update,
        )

        cohort = _cohort_with_members("s1")
        dist = init_distribution(cohort=cohort, initial_version="1.2.0")

        with pytest.raises(ValueError, match="downgrade"):
            publish_update(dist, cohort=cohort, new_version="1.1.0",
                           notes="x")


class TestAcceptUpdate:
    def test_student_accepts_update(self):
        from axiom.extensions.builtins.classroom.pack_distribution import (
            accept_update,
            init_distribution,
            publish_update,
        )

        cohort = _cohort_with_members("s1", "s2")
        dist = init_distribution(cohort=cohort, initial_version="1.0.0")
        dist = publish_update(dist, cohort=cohort, new_version="1.1.0", notes="x")

        assert dist.pinned_versions["s1"] == "1.0.0"

        dist = accept_update(dist, student_id="s1")

        assert dist.pinned_versions["s1"] == "1.1.0"
        assert "s1" not in dist.pending_updates
        assert "s2" in dist.pending_updates  # s2 still on 1.0.0

    def test_accept_without_pending_noop(self):
        from axiom.extensions.builtins.classroom.pack_distribution import (
            accept_update,
            init_distribution,
        )

        cohort = _cohort_with_members("s1")
        dist = init_distribution(cohort=cohort, initial_version="1.0.0")

        # No pending update — accept returns unchanged
        new_dist = accept_update(dist, student_id="s1")
        assert new_dist.pinned_versions["s1"] == "1.0.0"
        assert new_dist.pending_updates == {}


class TestActiveVersionLookup:
    def test_per_student_version_lookup(self):
        from axiom.extensions.builtins.classroom.pack_distribution import (
            accept_update,
            init_distribution,
            pack_version_for_student,
            publish_update,
        )

        cohort = _cohort_with_members("s1", "s2")
        dist = init_distribution(cohort=cohort, initial_version="1.0.0")
        dist = publish_update(dist, cohort=cohort, new_version="1.1.0", notes="x")
        dist = accept_update(dist, student_id="s1")

        assert pack_version_for_student(dist, "s1") == "1.1.0"
        assert pack_version_for_student(dist, "s2") == "1.0.0"


class TestTraceAnnotation:
    def test_annotate_trace_with_pack_version(self):
        from axiom.extensions.builtins.classroom.pack_distribution import (
            annotate_trace_with_pack_version,
            init_distribution,
        )

        cohort = _cohort_with_members("s1")
        dist = init_distribution(cohort=cohort, initial_version="1.0.0")

        trace = {"trace_id": "t1", "student_id": "s1", "content": "hi"}
        annotated = annotate_trace_with_pack_version(trace, dist)

        assert annotated["pack_version"] == "1.0.0"

    def test_annotate_batch(self):
        from axiom.extensions.builtins.classroom.pack_distribution import (
            accept_update,
            annotate_traces_with_pack_version,
            init_distribution,
            publish_update,
        )

        cohort = _cohort_with_members("s1", "s2")
        dist = init_distribution(cohort=cohort, initial_version="1.0.0")
        dist = publish_update(dist, cohort=cohort, new_version="1.1.0", notes="x")
        dist = accept_update(dist, "s1")

        traces = [
            {"trace_id": "t1", "student_id": "s1"},
            {"trace_id": "t2", "student_id": "s2"},
        ]
        out = annotate_traces_with_pack_version(traces, dist)
        assert out[0]["pack_version"] == "1.1.0"
        assert out[1]["pack_version"] == "1.0.0"


class TestBroadcastPayload:
    """What actually goes over the federation wire for a pack update."""

    def test_update_broadcast_includes_signature_slot(self):
        from axiom.extensions.builtins.classroom.pack_distribution import (
            build_update_broadcast,
            init_distribution,
            publish_update,
        )

        cohort = _cohort_with_members("s1", "s2")
        dist = init_distribution(cohort=cohort, initial_version="1.0.0")
        dist = publish_update(dist, cohort=cohort, new_version="1.1.0",
                              notes="added ch5")

        payload = build_update_broadcast(
            dist=dist, cohort=cohort, pack_path="./course.axiompack"
        )
        assert payload["classroom_id"] == "cr"
        assert payload["coordinator_node"] == "hub"
        assert payload["new_version"] == "1.1.0"
        assert payload["pack_path"] == "./course.axiompack"
        assert set(payload["recipients"]) == {"s1-node", "s2-node"}
        assert "signature" in payload
