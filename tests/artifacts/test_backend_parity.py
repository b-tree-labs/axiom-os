# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Both artifact backends must answer the same question the same way.

`ArtifactRegistry.delete()` funnels into `mark_deleted`, and the two backends
disagreed about a missing id:

    InMemoryBackend   raises KeyError
    SQLiteBackend     UPDATE ... WHERE id = ? matches zero rows, commits,
                      and returns None

Measured, not inferred: sqlite3 reports `rowcount == 0` and raises nothing.

That disagreement is worse than either behaviour on its own, because tests run
on the in-memory backend and installs run on SQLite. A test asserting "deleting
a missing artifact raises" passes forever while production silently does
nothing — a green check that cannot fail in the environment that matters.

The consequence lands in `memory.dedup.unmerge`, which discards every
`registry.delete()` result and then writes `outcome="ok"` to the audit log. On a
ledger where a delete can silently match nothing, that records a successful
unmerge for an unmerge that did not happen, in the log that exists to be
trusted when the code cannot be.

These tests run both backends through identical assertions, so the two cannot
drift apart again without one of them going red.
"""

from __future__ import annotations

import pytest

from axiom.artifacts.registry import (
    ArtifactRegistry,
    InMemoryBackend,
    SQLiteBackend,
)


@pytest.fixture(params=["memory", "sqlite"])
def registry(request, tmp_path):
    backend = (
        InMemoryBackend()
        if request.param == "memory"
        else SQLiteBackend(tmp_path / "artifacts.db")
    )
    return ArtifactRegistry(backend=backend)


def _register(reg) -> str:
    return reg.register(kind="alias", name="a1", data={"x": 1})


def test_deleting_a_missing_artifact_raises_on_every_backend(registry):
    """The divergence itself: SQLite used to commit a zero-row UPDATE."""
    with pytest.raises(KeyError):
        registry.delete("no-such-artifact-id", reason="unmerged")


def test_deleting_a_real_artifact_succeeds_on_every_backend(registry):
    art_id = _register(registry)
    registry.delete(art_id, reason="unmerged")
    assert registry.get(art_id).deleted is True


def test_a_tombstoned_artifact_can_be_deleted_again_on_every_backend(registry):
    """Whatever double-delete does, both backends must do the same thing."""
    art_id = _register(registry)
    registry.delete(art_id, reason="unmerged")
    registry.delete(art_id, reason="unmerged")
    assert registry.get(art_id).deleted is True
