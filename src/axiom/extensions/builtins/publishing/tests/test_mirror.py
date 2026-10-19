# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The mirror engine exists because of a real week: a human editing a document
in a remote web editor while an agent edited the synced local file, and every
surface silently overwriting the others. Stale editor sessions flushed old
buffers on close and on wake; the sync client wedged in one direction; whoever
wrote last won, unrecorded.

So the contract under test is directionality made explicit. One side is
canonical per mode; the other follows. Writes carry the version they expect
and abort on a foreign one. A remote that suddenly matches an OLD local base
is a stale-session flush, not new work, and gets repaired rather than
followed. Both-sides-changed with overlap holds and reports instead of
guessing. And when the mirror lives in a git repository, every sync event is
a commit with provenance trailers, so git operations see annotated history
rather than mystery edits.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from axiom.extensions.builtins.publishing.mirror import (
    MirrorEngine,
    RemoteDoc,
    StaleFlushDetected,
    VersionConflict,
)


class FakeEndpoint:
    """In-memory remote editor: versioned text, conflict-checked writes."""

    def __init__(self, text: str = "v1 text\n"):
        self.history = [RemoteDoc(text=text, version="1", author_app="test")]
        self.write_calls = 0

    @property
    def current(self) -> RemoteDoc:
        return self.history[-1]

    def read(self) -> RemoteDoc:
        return self.current

    def write(self, text: str, expected_version: str) -> str:
        self.write_calls += 1
        if expected_version != self.current.version:
            raise VersionConflict(expected_version, self.current.version)
        new = RemoteDoc(text=text, version=str(int(self.current.version) + 1),
                        author_app="agent")
        self.history.append(new)
        return new.version

    def edit_remotely(self, text: str, author_app: str = "web") -> None:
        """A human (or a stale session) saves on the remote side."""
        self.history.append(RemoteDoc(text=text,
                                      version=str(int(self.current.version) + 1),
                                      author_app=author_app))

    def versions(self, limit: int = 10) -> list[RemoteDoc]:
        return list(reversed(self.history[-limit:]))


@pytest.fixture
def engine(tmp_path):
    endpoint = FakeEndpoint()
    mirror = tmp_path / "doc.md"
    eng = MirrorEngine(endpoint=endpoint, mirror_path=mirror,
                       state_path=tmp_path / "state.json")
    eng.reconcile()  # initial pull establishes the base
    return eng, endpoint, mirror


class TestReconcile:
    def test_initial_pull_creates_mirror(self, engine):
        eng, endpoint, mirror = engine
        assert mirror.read_text() == "v1 text\n"

    def test_remote_change_pulls(self, engine):
        eng, endpoint, mirror = engine
        endpoint.edit_remotely("v2 from the web\n")
        report = eng.reconcile()
        assert report.action == "pull"
        assert mirror.read_text() == "v2 from the web\n"

    def test_local_change_pushes_with_version_check(self, engine):
        eng, endpoint, mirror = engine
        mirror.write_text("v2 from the agent\n")
        report = eng.reconcile()
        assert report.action == "push"
        assert endpoint.current.text == "v2 from the agent\n"

    def test_no_change_is_noop(self, engine):
        eng, endpoint, mirror = engine
        assert eng.reconcile().action == "noop"

    def test_push_races_foreign_write_then_converges(self, engine):
        eng, endpoint, mirror = engine
        mirror.write_text("agent line\n")
        # a human save lands between our read and our write
        original_read = endpoint.read
        def read_then_race():
            doc = original_read()
            if endpoint.write_calls == 0:
                endpoint.edit_remotely("human line\n")
            return doc
        endpoint.read = read_then_race
        report = eng.reconcile()
        # the conflicting push must NOT silently win: the engine re-reads and
        # reports the divergence instead of overwriting the human's line
        assert report.action in ("conflict", "pull")
        assert "human line" in endpoint.current.text

    def test_both_changed_overlapping_holds_and_reports(self, engine):
        eng, endpoint, mirror = engine
        endpoint.edit_remotely("v1 text edited remotely\n")
        mirror.write_text("v1 text edited locally\n")
        report = eng.reconcile()
        assert report.action == "conflict"
        # neither side silently lost: the canonical remote is untouched and
        # lands in the mirror; the local edit survives as the conflict copy
        assert endpoint.current.text == "v1 text edited remotely\n"
        assert mirror.read_text() == "v1 text edited remotely\n"
        assert report.conflict_copy is not None
        assert Path(report.conflict_copy).read_text() == "v1 text edited locally\n"


class TestStaleFlushRepair:
    def test_remote_reverting_to_old_base_is_repaired(self, engine):
        eng, endpoint, mirror = engine
        endpoint.edit_remotely("good newer text\n")
        eng.reconcile()  # pull: base is now the good text
        # a stale editor session flushes the ORIGINAL text back
        endpoint.edit_remotely("v1 text\n", author_app="stale-pane")
        report = eng.watch_once()
        assert isinstance(report.detected, StaleFlushDetected)
        assert report.detected.author_app == "stale-pane"
        assert endpoint.current.text == "good newer text\n"

    def test_genuine_remote_edit_is_not_treated_as_stale(self, engine):
        eng, endpoint, mirror = engine
        endpoint.edit_remotely("genuinely new content\n")
        report = eng.watch_once()
        assert report.detected is None
        assert mirror.read_text() == "genuinely new content\n"


class TestGitAnnotation:
    @pytest.fixture
    def git_repo(self, tmp_path, monkeypatch):
        # isolate from the user's global git config (house rule)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.org"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        return repo

    def test_pull_commits_with_provenance_trailers(self, git_repo, tmp_path):
        endpoint = FakeEndpoint()
        mirror = git_repo / "doc.md"
        eng = MirrorEngine(endpoint=endpoint, mirror_path=mirror,
                           state_path=tmp_path / "state.json", git_annotate=True)
        eng.reconcile()
        endpoint.edit_remotely("newer\n")
        eng.reconcile()
        log = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=git_repo,
                             capture_output=True, text=True, check=True).stdout
        assert "Synced-From:" in log
        assert "Endpoint-Version: 2" in log

    def test_no_repo_means_no_git_calls(self, engine):
        eng, endpoint, mirror = engine
        endpoint.edit_remotely("newer\n")
        report = eng.reconcile()
        assert report.git_commit is None
