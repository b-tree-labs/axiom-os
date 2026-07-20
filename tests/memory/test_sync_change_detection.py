# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for P4 change detection (ADR-087 D2, PRD F6, scope item 1).

Per-harness change detectors are the P2 absorb adapters run in *watch
mode*: mtime + content-hash over markdown / structured stores. They emit
source-native change events and NEVER write the source (D8 read-only).
The managed write-back region is stripped before a change is derived, so
a fragment we wrote out is not read back as an inbound edit (the marker
half of echo suppression).
"""

from __future__ import annotations

from pathlib import Path

from axiom.memory.absorb.markdown_hierarchy import agents_md_adapter
from axiom.memory.rendering import (
    EpochSnapshot,
    PreambleEntry,
    render_agents_md_block,
)
from axiom.memory.sync.detect import ChangeDetector


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _detector(root: Path, *, harness: str = "agents-md") -> ChangeDetector:
    adapter = agents_md_adapter(account="acct-a", roots=[root])
    return ChangeDetector(adapter=adapter)


class TestFirstPollDetectsEverything:
    def test_new_file_is_a_change(self, tmp_path: Path):
        _write(tmp_path / "AGENTS.md", "# Rules\n\nAlways run tests.\n")
        det = _detector(tmp_path)
        changes = det.poll()
        assert len(changes) == 1
        change = changes[0]
        assert change.harness == "agents-md"
        assert change.account == "acct-a"
        assert change.source_ref == str(tmp_path / "AGENTS.md")
        assert len(change.candidates) == 1

    def test_second_poll_no_change_is_empty(self, tmp_path: Path):
        _write(tmp_path / "AGENTS.md", "# Rules\n\nAlways run tests.\n")
        det = _detector(tmp_path)
        assert len(det.poll()) == 1
        assert det.poll() == []  # nothing changed → no event


class TestContentChangeDetection:
    def test_edited_content_is_a_change(self, tmp_path: Path):
        path = tmp_path / "AGENTS.md"
        _write(path, "# Rules\n\nAlways run tests.\n")
        det = _detector(tmp_path)
        det.poll()
        _write(path, "# Rules\n\nAlways run tests. Prefer ruff.\n")
        changes = det.poll()
        assert len(changes) == 1
        assert changes[0].source_ref == str(path)

    def test_change_id_stable_for_same_content(self, tmp_path: Path):
        path = tmp_path / "AGENTS.md"
        _write(path, "hello\n")
        c1 = _detector(tmp_path).poll()[0]
        c2 = _detector(tmp_path).poll()[0]
        assert c1.change_id == c2.change_id

    def test_change_id_differs_on_edit(self, tmp_path: Path):
        path = tmp_path / "AGENTS.md"
        _write(path, "hello\n")
        det = _detector(tmp_path)
        first = det.poll()[0]
        _write(path, "hello world\n")
        second = det.poll()[0]
        assert first.change_id != second.change_id


class TestManagedBlockStrippedEchoSuppression:
    def test_our_write_back_is_not_a_source_change(self, tmp_path: Path):
        """Writing our managed block into a file the user also edited must not
        register the block itself as inbound source content."""
        path = tmp_path / "AGENTS.md"
        _write(path, "# Rules\n\nAlways run tests.\n")
        det = _detector(tmp_path)
        det.poll()  # establish baseline on the user's authored content

        # Splice our managed block in, as write-back would.
        snap = EpochSnapshot(
            session_id="s", epoch=0,
            entries=(PreambleEntry("f1", "user prefers vim"),),
        )
        block = render_agents_md_block(snap)
        _write(path, path.read_text() + "\n" + block + "\n")

        # The only delta is our own block → no source change detected.
        assert det.poll() == []

    def test_file_that_is_only_our_block_never_detects(self, tmp_path: Path):
        """A fresh instruction file we created (block only) is not a source."""
        path = tmp_path / "AGENTS.md"
        snap = EpochSnapshot(
            session_id="s", epoch=0,
            entries=(PreambleEntry("f1", "synced from peer"),),
        )
        _write(path, render_agents_md_block(snap) + "\n")
        det = _detector(tmp_path)
        assert det.poll() == []


class TestReadOnly:
    def test_poll_never_writes_the_source(self, tmp_path: Path):
        path = tmp_path / "AGENTS.md"
        _write(path, "# Rules\n\nAlways run tests.\n")
        before_mtime = path.stat().st_mtime_ns
        before_bytes = path.read_bytes()
        det = _detector(tmp_path)
        det.poll()
        det.poll()
        assert path.stat().st_mtime_ns == before_mtime
        assert path.read_bytes() == before_bytes
