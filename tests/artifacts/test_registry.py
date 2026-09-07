# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ArtifactRegistry — content-addressed store for Course, Classroom, Eval, etc."""

from __future__ import annotations


def test_register_and_get() -> None:
    from axiom.artifacts import ArtifactRegistry

    r = ArtifactRegistry()
    aid = r.register(kind="course", name="NE101", data={"credits": 3})
    got = r.get(aid)
    assert got.kind == "course"
    assert got.name == "NE101"
    assert got.data == {"credits": 3}


def test_list_by_kind() -> None:
    from axiom.artifacts import ArtifactRegistry

    r = ArtifactRegistry()
    r.register(kind="course", name="A", data={})
    r.register(kind="course", name="B", data={})
    r.register(kind="classroom", name="A-S26", data={})
    courses = r.list(kind="course")
    assert {a.name for a in courses} == {"A", "B"}


def test_delete_marks_tombstone() -> None:
    from axiom.artifacts import ArtifactRegistry

    r = ArtifactRegistry()
    aid = r.register(kind="course", name="X", data={})
    r.delete(aid, reason="user-requested")
    got = r.get(aid)
    assert got.deleted is True
    assert got.deletion_reason == "user-requested"
    # Deleted artifacts are excluded from list() by default.
    assert r.list(kind="course") == []
    assert len(r.list(kind="course", include_deleted=True)) == 1


def test_content_hash_is_deterministic() -> None:
    from axiom.artifacts import ArtifactRegistry

    r = ArtifactRegistry()
    a1 = r.register(kind="eval", name="faith", data={"threshold": 0.9})
    a2 = r.register(kind="eval", name="faith", data={"threshold": 0.9})
    # Distinct ids (different creation time) but same content hash.
    assert a1 != a2
    assert r.get(a1).content_hash == r.get(a2).content_hash


def test_register_signs_artifact_when_key_provided() -> None:
    from axiom.artifacts import ArtifactRegistry

    r = ArtifactRegistry(signer=lambda payload: b"sig:" + payload[:8])
    aid = r.register(kind="finding", name="F1", data={"claim": "X"})
    a = r.get(aid)
    assert a.signature is not None
    assert a.signature.startswith(b"sig:")
