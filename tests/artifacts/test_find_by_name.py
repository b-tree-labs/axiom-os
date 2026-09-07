# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Keyed (kind, name) lookup on the artifact registry (ADR-087 P1).

``find_by_name`` pushes the name predicate into the backend so callers
stop doing list-then-filter over every row of a kind. Semantics are
pinned to match the scan paths they replace: non-deleted only by
default, ``created_at`` ascending, ``include_deleted=True`` for
version-chain use."""

from __future__ import annotations

import pytest


def _registry_sqlite(tmp_path):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend

    return ArtifactRegistry(backend=SQLiteBackend(tmp_path / "a.db"))


def _registry_memory(tmp_path):
    from axiom.artifacts.registry import ArtifactRegistry, InMemoryBackend

    return ArtifactRegistry(backend=InMemoryBackend())


@pytest.fixture(params=["sqlite", "memory"])
def registry(request, tmp_path):
    make = _registry_sqlite if request.param == "sqlite" else _registry_memory
    return make(tmp_path)


def _seed(registry):
    a1 = registry.register(kind="fragment", name="frag-a", data={"v": 1})
    a2 = registry.register(kind="fragment", name="frag-a", data={"v": 2})
    b1 = registry.register(kind="fragment", name="frag-b", data={"v": 3})
    o1 = registry.register(kind="other", name="frag-a", data={"v": 4})
    return a1, a2, b1, o1


class TestFindByName:
    def test_returns_only_matching_kind_and_name(self, registry):
        _seed(registry)
        got = registry.find_by_name(kind="fragment", name="frag-a")
        assert [a.data["v"] for a in got] == [1, 2]
        assert all(a.kind == "fragment" and a.name == "frag-a" for a in got)

    def test_created_at_ascending(self, registry):
        _seed(registry)
        got = registry.find_by_name(kind="fragment", name="frag-a")
        assert [a.created_at for a in got] == sorted(
            a.created_at for a in got
        )

    def test_excludes_deleted_by_default(self, registry):
        a1, a2, _, _ = _seed(registry)
        registry.delete(a1, reason="test")
        got = registry.find_by_name(kind="fragment", name="frag-a")
        assert [a.data["v"] for a in got] == [2]

    def test_include_deleted_returns_tombstones(self, registry):
        a1, a2, _, _ = _seed(registry)
        registry.delete(a1, reason="test")
        got = registry.find_by_name(
            kind="fragment", name="frag-a", include_deleted=True
        )
        assert [a.data["v"] for a in got] == [1, 2]
        assert got[0].deleted and not got[1].deleted

    def test_missing_name_returns_empty(self, registry):
        _seed(registry)
        assert registry.find_by_name(kind="fragment", name="nope") == []

    def test_matches_scan_equivalent(self, registry):
        """Keyed lookup returns exactly what the old scan returned."""
        _seed(registry)
        scan = [
            a for a in registry.list(kind="fragment") if a.name == "frag-a"
        ]
        keyed = registry.find_by_name(kind="fragment", name="frag-a")
        assert [a.id for a in keyed] == [a.id for a in scan]
