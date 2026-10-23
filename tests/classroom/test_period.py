# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Period — a scheduled class meeting inside a Classroom.

Phase 2 Classroom. A Period scopes RACI grants, presence, and transient
policy. "@all-curios during this period, prioritize reactor kinetics" —
policy expires automatically when the period ends.
"""

from __future__ import annotations


def test_schedule_period() -> None:
    from axiom.artifacts import ArtifactRegistry
    from axiom.classroom import ClassroomService, CourseService, PeriodService

    reg = ArtifactRegistry()
    courses = CourseService(registry=reg)
    rooms = ClassroomService(registry=reg, courses=courses)
    periods = PeriodService(registry=reg, classrooms=rooms)

    cid = courses.create(name="NE101", owner="@ben")
    rid = rooms.open(course_id=cid, term="S26", instructor="@ben")

    pid = periods.schedule(classroom_id=rid, title="Lecture 1", starts_at=100.0, ends_at=200.0)
    p = periods.get(pid)
    assert p.classroom_id == rid
    assert p.title == "Lecture 1"
    assert p.starts_at == 100.0
    assert p.ends_at == 200.0
    assert p.status == "scheduled"


def test_start_and_end_period() -> None:
    from axiom.artifacts import ArtifactRegistry
    from axiom.classroom import ClassroomService, CourseService, PeriodService

    reg = ArtifactRegistry()
    courses = CourseService(registry=reg)
    rooms = ClassroomService(registry=reg, courses=courses)
    periods = PeriodService(registry=reg, classrooms=rooms)
    cid = courses.create(name="NE101", owner="@ben")
    rid = rooms.open(course_id=cid, term="S26", instructor="@ben")
    pid = periods.schedule(classroom_id=rid, title="L1", starts_at=0.0, ends_at=100.0)

    periods.start(pid, now=1.0)
    assert periods.get(pid).status == "in_progress"

    periods.end(pid, now=50.0)
    p = periods.get(pid)
    assert p.status == "ended"
    assert p.actual_ends_at == 50.0


def test_period_presence_join_and_leave() -> None:
    from axiom.artifacts import ArtifactRegistry
    from axiom.classroom import ClassroomService, CourseService, PeriodService

    reg = ArtifactRegistry()
    courses = CourseService(registry=reg)
    rooms = ClassroomService(registry=reg, courses=courses)
    periods = PeriodService(registry=reg, classrooms=rooms)
    cid = courses.create(name="NE101", owner="@ben")
    rid = rooms.open(course_id=cid, term="S26", instructor="@ben")
    rooms.enroll(rid, student="@alice")
    pid = periods.schedule(classroom_id=rid, title="L1", starts_at=0.0, ends_at=100.0)

    periods.start(pid, now=1.0)
    periods.join(pid, participant="@alice", now=2.0)
    assert "@alice" in periods.present(pid)

    periods.leave(pid, participant="@alice", now=10.0)
    assert "@alice" not in periods.present(pid)


def test_cannot_join_ended_period() -> None:
    import pytest

    from axiom.artifacts import ArtifactRegistry
    from axiom.classroom import ClassroomService, CourseService, PeriodService

    reg = ArtifactRegistry()
    courses = CourseService(registry=reg)
    rooms = ClassroomService(registry=reg, courses=courses)
    periods = PeriodService(registry=reg, classrooms=rooms)
    cid = courses.create(name="NE101", owner="@ben")
    rid = rooms.open(course_id=cid, term="S26", instructor="@ben")
    pid = periods.schedule(classroom_id=rid, title="L1", starts_at=0.0, ends_at=100.0)
    periods.start(pid, now=1.0)
    periods.end(pid, now=50.0)

    with pytest.raises(RuntimeError, match="not in progress"):
        periods.join(pid, participant="@late", now=51.0)


def test_list_periods_for_classroom() -> None:
    from axiom.artifacts import ArtifactRegistry
    from axiom.classroom import ClassroomService, CourseService, PeriodService

    reg = ArtifactRegistry()
    courses = CourseService(registry=reg)
    rooms = ClassroomService(registry=reg, courses=courses)
    periods = PeriodService(registry=reg, classrooms=rooms)
    cid = courses.create(name="NE101", owner="@ben")
    rid = rooms.open(course_id=cid, term="S26", instructor="@ben")
    periods.schedule(classroom_id=rid, title="L1", starts_at=0.0, ends_at=100.0)
    periods.schedule(classroom_id=rid, title="L2", starts_at=200.0, ends_at=300.0)

    listed = periods.list_for_classroom(rid)
    titles = {p.title for p in listed}
    assert titles == {"L1", "L2"}
