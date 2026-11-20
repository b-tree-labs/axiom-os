# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for persistent ArtifactRegistry (#41).

SQLiteBackend persists artifacts + tombstones across process restarts.
Version chain: multiple artifacts with the same (kind, name) form a
chain; latest() resolves to the non-deleted head.
"""

from __future__ import annotations


class TestSQLiteBackendRoundTrip:
    def test_register_and_get_across_instances(self, tmp_path):
        from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend

        db = tmp_path / "artifacts.db"

        reg1 = ArtifactRegistry(backend=SQLiteBackend(db))
        aid = reg1.register(
            kind="course",
            name="ne-prague-2026",
            data={"title": "NE Prague 2026"},
        )

        # Fresh instance against the same DB
        reg2 = ArtifactRegistry(backend=SQLiteBackend(db))
        art = reg2.get(aid)
        assert art.id == aid
        assert art.kind == "course"
        assert art.name == "ne-prague-2026"
        assert art.data == {"title": "NE Prague 2026"}


class TestListFilters:
    def test_list_by_kind_persisted(self, tmp_path):
        from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend

        reg = ArtifactRegistry(backend=SQLiteBackend(tmp_path / "a.db"))
        reg.register(kind="course", name="a", data={})
        reg.register(kind="course", name="b", data={})
        reg.register(kind="classroom", name="c", data={})

        courses = reg.list(kind="course")
        assert len(courses) == 2

    def test_deleted_excluded_by_default(self, tmp_path):
        from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend

        reg = ArtifactRegistry(backend=SQLiteBackend(tmp_path / "a.db"))
        aid = reg.register(kind="note", name="x", data={})
        reg.delete(aid, reason="superseded")

        assert reg.list(kind="note") == []
        assert len(reg.list(kind="note", include_deleted=True)) == 1


class TestVersionChain:
    def test_new_content_same_name_creates_version_2(self, tmp_path):
        from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend

        reg = ArtifactRegistry(backend=SQLiteBackend(tmp_path / "a.db"))
        reg.register(kind="course", name="ne-prague-2026", data={"version": "1.0.0"})
        reg.register(kind="course", name="ne-prague-2026", data={"version": "1.1.0"})

        chain = reg.version_chain(kind="course", name="ne-prague-2026")
        assert len(chain) == 2
        # Earliest first
        assert chain[0].data["version"] == "1.0.0"
        assert chain[1].data["version"] == "1.1.0"

    def test_latest_resolves_to_head(self, tmp_path):
        from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend

        reg = ArtifactRegistry(backend=SQLiteBackend(tmp_path / "a.db"))
        reg.register(kind="course", name="ne-prague", data={"version": "1.0.0"})
        v2 = reg.register(kind="course", name="ne-prague", data={"version": "1.1.0"})

        latest = reg.latest(kind="course", name="ne-prague")
        assert latest.id == v2

    def test_latest_skips_deleted(self, tmp_path):
        from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend

        reg = ArtifactRegistry(backend=SQLiteBackend(tmp_path / "a.db"))
        v1 = reg.register(kind="course", name="ne-prague", data={"version": "1.0.0"})
        v2 = reg.register(kind="course", name="ne-prague", data={"version": "1.1.0"})
        reg.delete(v2, reason="bad-publish")

        latest = reg.latest(kind="course", name="ne-prague")
        assert latest.id == v1

    def test_no_chain_returns_none(self, tmp_path):
        from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend

        reg = ArtifactRegistry(backend=SQLiteBackend(tmp_path / "a.db"))
        assert reg.latest(kind="x", name="does-not-exist") is None


class TestInMemoryBackendStillWorks:
    """Backward compat: InMemoryBackend == old behavior."""

    def test_in_memory_default_backend(self):
        from axiom.artifacts.registry import ArtifactRegistry

        reg = ArtifactRegistry()  # no backend → in-memory
        aid = reg.register(kind="note", name="n1", data={"text": "x"})
        art = reg.get(aid)
        assert art.name == "n1"


class TestSignaturePreserved:
    def test_signature_persists(self, tmp_path):
        from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend

        def fake_signer(b):
            return b"SIG:" + b[:8]

        reg1 = ArtifactRegistry(
            backend=SQLiteBackend(tmp_path / "a.db"), signer=fake_signer
        )
        aid = reg1.register(kind="course", name="x", data={"v": "1"})

        reg2 = ArtifactRegistry(backend=SQLiteBackend(tmp_path / "a.db"))
        art = reg2.get(aid)
        assert art.signature is not None
        assert art.signature.startswith(b"SIG:")
