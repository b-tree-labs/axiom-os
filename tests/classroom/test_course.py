# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Course — a reusable template. Classroom is an instance of a Course."""

from __future__ import annotations


def test_create_course() -> None:
    from axiom.artifacts import ArtifactRegistry
    from axiom.classroom import CourseService

    svc = CourseService(registry=ArtifactRegistry())
    cid = svc.create(name="NE101", description="Intro to Nuclear Eng", owner="@ben:ut-austin")
    c = svc.get(cid)
    assert c.name == "NE101"
    assert c.owner == "@ben:ut-austin"


def test_list_courses_excludes_deleted() -> None:
    from axiom.artifacts import ArtifactRegistry
    from axiom.classroom import CourseService

    svc = CourseService(registry=ArtifactRegistry())
    a = svc.create(name="A", owner="@o")
    svc.create(name="B", owner="@o")
    svc.delete(a, reason="typo")
    names = {c.name for c in svc.list()}
    assert names == {"B"}


def test_course_delete_then_get_shows_tombstone() -> None:
    from axiom.artifacts import ArtifactRegistry
    from axiom.classroom import CourseService

    svc = CourseService(registry=ArtifactRegistry())
    cid = svc.create(name="X", owner="@o")
    svc.delete(cid, reason="obsolete")
    c = svc.get(cid)
    assert c.deleted is True
    assert c.deletion_reason == "obsolete"
