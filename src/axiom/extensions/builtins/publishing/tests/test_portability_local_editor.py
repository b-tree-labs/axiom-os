# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Portability proof (ADR-110 / P4): a second, non-Microsoft document-editor
backend is one `register_editor` call, and the mirror engine works against it
UNCHANGED — same RemoteEditorEndpoint contract, same optimistic-concurrency
writes. Nothing above the connector fabric knows it isn't Graph."""

from __future__ import annotations

from axiom.extensions.builtins.publishing import mirror as mirror_mod
from axiom.extensions.builtins.publishing.providers import editors
from axiom.extensions.builtins.publishing.providers.local_editor import LocalFileEditor


def test_local_editor_is_registered_and_routed():
    assert "local" in editors.available_editors()
    assert editors.resolve_editor_vendor("file:///tmp/doc.md") == "local"


def test_mirror_full_cycle_against_a_non_graph_backend(tmp_path):
    """Pull, push, and a conflict — the whole doctrine — on a local-file remote,
    proving the engine is backend-agnostic and optimistic concurrency holds."""
    remote = tmp_path / "remote.md"
    endpoint = editors.get_editor("local", url=str(remote))
    endpoint.human_save("v1 from the remote\n")           # seed the remote

    mirror = tmp_path / "mirror.md"
    engine = mirror_mod.MirrorEngine(
        endpoint=endpoint, mirror_path=mirror,
        state_path=tmp_path / "state.json")

    # PULL: remote has content, local is empty -> mirror gets it
    assert engine.reconcile().action == "pull"
    assert mirror.read_text() == "v1 from the remote\n"

    # PUSH: edit the mirror locally -> lands on the remote with a bumped version
    mirror.write_text("edited locally\n")
    r = engine.reconcile()
    assert r.action == "push"
    assert endpoint.read().text == "edited locally\n"

    # CONVERGED
    assert engine.reconcile().action == "noop"

    # CONFLICT: both sides move -> mirror follows canonical, local preserved
    endpoint.human_save("remote moved\n")
    mirror.write_text("local also moved\n")
    r = engine.reconcile()
    assert r.action == "conflict"
    assert r.conflict_copy is not None
    assert mirror.read_text() == "remote moved\n"          # canonical wins the slot


def test_write_refuses_a_stale_expected_version(tmp_path):
    """Optimistic concurrency on the non-Graph backend: a write with the wrong
    expected version raises VersionConflict, never a blind overwrite."""
    remote = tmp_path / "r.md"
    ep = LocalFileEditor(path=str(remote))
    v1 = ep.write("first\n", "0")
    ep.human_save("someone else\n")                        # remote moves past v1
    import pytest
    with pytest.raises(mirror_mod.VersionConflict):
        ep.write("stale\n", v1)                            # v1 is no longer current
