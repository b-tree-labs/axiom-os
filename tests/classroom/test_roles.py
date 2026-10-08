# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom roles: instructor, TA, student, observer."""

from __future__ import annotations


def test_enroll_with_role() -> None:
    from axiom.artifacts import ArtifactRegistry
    from axiom.classroom import ClassroomService, CourseService

    reg = ArtifactRegistry()
    courses = CourseService(registry=reg)
    rooms = ClassroomService(registry=reg, courses=courses)
    cid = courses.create(name="NE101", owner="@ben")
    rid = rooms.open(course_id=cid, term="S26", instructor="@ben")

    rooms.enroll(rid, student="@alice", role="student")
    rooms.enroll(rid, student="@ta", role="ta")
    rooms.enroll(rid, student="@obs", role="observer")

    roles = rooms.roles(rid)
    assert roles["@alice"] == "student"
    assert roles["@ta"] == "ta"
    assert roles["@obs"] == "observer"


def test_default_role_is_student() -> None:
    from axiom.artifacts import ArtifactRegistry
    from axiom.classroom import ClassroomService, CourseService

    reg = ArtifactRegistry()
    courses = CourseService(registry=reg)
    rooms = ClassroomService(registry=reg, courses=courses)
    cid = courses.create(name="NE101", owner="@ben")
    rid = rooms.open(course_id=cid, term="S26", instructor="@ben")

    rooms.enroll(rid, student="@alice")
    assert rooms.roles(rid)["@alice"] == "student"


def test_unenroll() -> None:
    from axiom.artifacts import ArtifactRegistry
    from axiom.classroom import ClassroomService, CourseService

    reg = ArtifactRegistry()
    courses = CourseService(registry=reg)
    rooms = ClassroomService(registry=reg, courses=courses)
    cid = courses.create(name="NE101", owner="@ben")
    rid = rooms.open(course_id=cid, term="S26", instructor="@ben")
    rooms.enroll(rid, student="@alice")
    rooms.enroll(rid, student="@bob")

    rooms.unenroll(rid, student="@alice")
    room = rooms.get(rid)
    assert "@alice" not in room.roster
    assert "@bob" in room.roster


def test_invalid_role_rejected() -> None:
    import pytest

    from axiom.artifacts import ArtifactRegistry
    from axiom.classroom import ClassroomService, CourseService

    reg = ArtifactRegistry()
    courses = CourseService(registry=reg)
    rooms = ClassroomService(registry=reg, courses=courses)
    cid = courses.create(name="NE101", owner="@ben")
    rid = rooms.open(course_id=cid, term="S26", instructor="@ben")

    with pytest.raises(ValueError, match="invalid role"):
        rooms.enroll(rid, student="@alice", role="admin")
