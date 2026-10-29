# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom — a live instance of a Course for a specific term/cohort."""

from __future__ import annotations


def test_open_classroom_from_course() -> None:
    from axiom.artifacts import ArtifactRegistry
    from axiom.classroom import ClassroomService, CourseService

    reg = ArtifactRegistry()
    courses = CourseService(registry=reg)
    classrooms = ClassroomService(registry=reg, courses=courses)

    course_id = courses.create(name="NE101", owner="@ben:ut-austin")
    room_id = classrooms.open(
        course_id=course_id, term="Spring 2026", instructor="@ben:ut-austin"
    )

    room = classrooms.get(room_id)
    assert room.course_id == course_id
    assert room.term == "Spring 2026"
    assert room.instructor == "@ben:ut-austin"
    assert room.status == "open"
    assert room.roster == []


def test_enroll_students() -> None:
    from axiom.artifacts import ArtifactRegistry
    from axiom.classroom import ClassroomService, CourseService

    reg = ArtifactRegistry()
    courses = CourseService(registry=reg)
    rooms = ClassroomService(registry=reg, courses=courses)
    cid = courses.create(name="NE101", owner="@ben")
    rid = rooms.open(course_id=cid, term="S26", instructor="@ben")

    rooms.enroll(rid, student="@alice:ut-austin")
    rooms.enroll(rid, student="@bob:ut-austin")
    room = rooms.get(rid)
    assert set(room.roster) == {"@alice:ut-austin", "@bob:ut-austin"}


def test_cannot_enroll_in_closed_classroom() -> None:
    import pytest

    from axiom.artifacts import ArtifactRegistry
    from axiom.classroom import ClassroomService, CourseService

    reg = ArtifactRegistry()
    courses = CourseService(registry=reg)
    rooms = ClassroomService(registry=reg, courses=courses)
    cid = courses.create(name="NE101", owner="@ben")
    rid = rooms.open(course_id=cid, term="S26", instructor="@ben")
    rooms.archive(rid, reason="end of term")

    with pytest.raises(RuntimeError, match="not open"):
        rooms.enroll(rid, student="@carol")


def test_archive_classroom_sets_status_and_timestamp() -> None:
    from axiom.artifacts import ArtifactRegistry
    from axiom.classroom import ClassroomService, CourseService

    reg = ArtifactRegistry()
    courses = CourseService(registry=reg)
    rooms = ClassroomService(registry=reg, courses=courses)
    cid = courses.create(name="NE101", owner="@ben")
    rid = rooms.open(course_id=cid, term="S26", instructor="@ben")
    rooms.enroll(rid, student="@alice")

    rooms.archive(rid, reason="term ended")
    room = rooms.get(rid)
    assert room.status == "archived"
    assert room.archive_reason == "term ended"
    assert room.archived_at is not None
    # Roster and history preserved (per ADR: classroom archive keeps 90d default).
    assert "@alice" in room.roster


def test_opening_classroom_requires_existing_course() -> None:
    import pytest

    from axiom.artifacts import ArtifactRegistry
    from axiom.classroom import ClassroomService, CourseService

    reg = ArtifactRegistry()
    courses = CourseService(registry=reg)
    rooms = ClassroomService(registry=reg, courses=courses)

    with pytest.raises(LookupError):
        rooms.open(course_id="nonexistent", term="S26", instructor="@ben")
