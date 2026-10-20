# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Block-on-conflict + the resolve verb (ADR-112 §D3).

A both-sides-moved conflict does not auto-converge: it lands the incoming
canonical text in the mirror, preserves the local edit in a ``.conflict``
sidecar, and *blocks* every further pass until a human runs ``resolve`` —
git's essence, made safe for a live byte-mirror. ``resolve`` takes one of
three sides: ``theirs`` (keep canonical), ``ours`` (push your edit), or
``merged`` (push whatever you hand-edited the mirror into).
"""

from __future__ import annotations

import threading

import pytest

from axiom.extensions.builtins.publishing.mirror import (
    MirrorEngine,
    RemoteDoc,
    VersionConflict,
)
from axiom.infra.conflict import ConflictOutcome


class Endpoint:
    """Minimal in-memory remote with version-checked writes."""

    def __init__(self, text: str = "origin\n"):
        self._lock = threading.Lock()
        self.history = [RemoteDoc(text=text, version="1", author_app="seed")]

    @property
    def current(self) -> RemoteDoc:
        return self.history[-1]

    def read(self) -> RemoteDoc:
        with self._lock:
            return self.current

    def write(self, text: str, expected_version: str) -> str:
        with self._lock:
            if expected_version != self.current.version:
                raise VersionConflict(expected_version, self.current.version)
            doc = RemoteDoc(text=text,
                            version=str(int(self.current.version) + 1),
                            author_app="agent")
            self.history.append(doc)
            return doc.version

    def human_save(self, text: str, app: str = "web") -> None:
        with self._lock:
            self.history.append(RemoteDoc(
                text=text, version=str(int(self.current.version) + 1),
                author_app=app))

    def versions(self, limit: int = 10) -> list[RemoteDoc]:
        with self._lock:
            return list(reversed(self.history[-limit:]))


def _engine(tmp_path):
    endpoint = Endpoint()
    mirror = tmp_path / "doc.md"
    engine = MirrorEngine(endpoint=endpoint, mirror_path=mirror,
                          state_path=tmp_path / "state.json")
    engine.reconcile()  # baseline on "origin\n"
    return engine, endpoint, mirror


def _drive_to_conflict(engine, endpoint, mirror):
    """Make both sides move to different content → a blocked conflict."""
    endpoint.human_save("remote wins here\n")
    mirror.write_text("local edit here\n")
    report = engine.reconcile()
    return report


def test_both_moved_blocks_and_preserves_both_sides(tmp_path):
    engine, endpoint, mirror = _engine(tmp_path)
    report = _drive_to_conflict(engine, endpoint, mirror)

    assert report.action == "conflict"
    assert isinstance(report.conflict, ConflictOutcome)
    assert report.conflict.blocked is True
    # canonical remote lands in the readable mirror; local kept in the sidecar
    assert mirror.read_text() == "remote wins here\n"
    sidecar = mirror.with_suffix(mirror.suffix + ".conflict")
    assert sidecar.read_text() == "local edit here\n"


def test_conflict_blocks_further_sync_until_resolved(tmp_path):
    engine, endpoint, mirror = _engine(tmp_path)
    _drive_to_conflict(engine, endpoint, mirror)

    # every further pass is a no-op "blocked" report — the engine does not
    # silently follow the remote again, does not touch the mirror
    for _ in range(3):
        r = engine.reconcile()
        assert r.action == "blocked"
        assert r.conflict is not None and r.conflict.blocked
    assert engine.watch_once().action == "blocked"
    # a remote that keeps moving is NOT pulled while blocked
    endpoint.human_save("later remote\n")
    assert engine.reconcile().action == "blocked"
    assert mirror.read_text() == "remote wins here\n"  # unchanged


def test_blocked_state_survives_a_fresh_engine(tmp_path):
    engine, endpoint, mirror = _engine(tmp_path)
    _drive_to_conflict(engine, endpoint, mirror)
    # a new process (new engine on the same state file) is still blocked
    reborn = MirrorEngine(endpoint=endpoint, mirror_path=mirror,
                          state_path=tmp_path / "state.json")
    assert reborn.reconcile().action == "blocked"


def test_resolve_theirs_keeps_canonical_and_converges(tmp_path):
    engine, endpoint, mirror = _engine(tmp_path)
    _drive_to_conflict(engine, endpoint, mirror)
    sidecar = mirror.with_suffix(mirror.suffix + ".conflict")

    r = engine.resolve("theirs")
    assert r.action == "resolved"
    assert mirror.read_text() == "remote wins here\n"
    assert not sidecar.exists()               # conflict artifact cleaned up
    assert engine.reconcile().action == "noop"  # unblocked + converged


def test_resolve_ours_pushes_local_and_converges(tmp_path):
    engine, endpoint, mirror = _engine(tmp_path)
    _drive_to_conflict(engine, endpoint, mirror)

    r = engine.resolve("ours")
    assert r.action == "resolved"
    assert endpoint.current.text == "local edit here\n"   # local is now canonical
    assert mirror.read_text() == "local edit here\n"
    assert engine.reconcile().action == "noop"
    assert not mirror.with_suffix(mirror.suffix + ".conflict").exists()


def test_resolve_merged_pushes_handedited_mirror(tmp_path):
    engine, endpoint, mirror = _engine(tmp_path)
    _drive_to_conflict(engine, endpoint, mirror)

    # the human reads both sides and writes a merge into the mirror by hand
    mirror.write_text("remote wins here\nlocal edit here\n")
    r = engine.resolve("merged")
    assert r.action == "resolved"
    assert endpoint.current.text == "remote wins here\nlocal edit here\n"
    assert engine.reconcile().action == "noop"


def test_resolve_ours_reblocks_when_remote_advanced(tmp_path):
    engine, endpoint, mirror = _engine(tmp_path)
    _drive_to_conflict(engine, endpoint, mirror)

    # someone edits the remote again before we resolve; "ours" cannot push blind
    endpoint.human_save("remote moved again\n")
    r = engine.resolve("ours")
    assert r.action == "conflict"          # re-opened against the fresh remote
    assert r.conflict.blocked is True
    # our intended edit is preserved, not lost
    sidecar_texts = {p.read_text()
                     for p in tmp_path.glob("doc.md.conflict*")}
    assert "local edit here\n" in sidecar_texts
    assert mirror.read_text() == "remote moved again\n"  # fresh canonical in file


def test_resolve_without_conflict_is_noop(tmp_path):
    engine, endpoint, mirror = _engine(tmp_path)
    assert engine.resolve("theirs").action == "noop"


def test_resolve_rejects_unknown_strategy(tmp_path):
    engine, endpoint, mirror = _engine(tmp_path)
    _drive_to_conflict(engine, endpoint, mirror)
    with pytest.raises(ValueError, match="theirs, ours, or merged"):
        engine.resolve("mine")


def test_conflict_outcome_guidance_is_actionable(tmp_path):
    engine, endpoint, mirror = _engine(tmp_path)
    report = _drive_to_conflict(engine, endpoint, mirror)
    text = report.conflict.guidance()
    assert "paused" in text and "theirs" in text and "ours" in text
    assert str(report.conflict.preserved_path) in text
