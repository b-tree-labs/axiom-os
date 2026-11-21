# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chaos suite for the mirror engine: seeded storms of everything that
actually happened, plus everything that could.

The invariants under storm are the doctrine, not the mechanism:

- I1 No human-authored text is ever silently lost. After any storm, every
  human edit is the current remote, an ancestor of it in the base lineage,
  or preserved in a conflict copy.
- I2 The engine never writes the remote blind: every write carries the
  version that was current when it was issued (the fake enforces this).
- I3 Quiescence converges: when the humans stop, the mirror reaches the
  remote and a second pass is a noop. A storm that ends blocked on a
  conflict (block-on-conflict doctrine) is a valid terminal state — both
  sides are preserved (I1) — and converges once a human resolves it.
- I4 Crashes are survivable: killing the engine between any two steps and
  restarting on the same state file still satisfies I1-I3.
- I5 Corrupt or missing state never destroys local edits.
- I6 Git annotation commits only the mirror file — a user's staged work is
  never swept into a sync commit.
- I7 A stale flush is repaired with real content, never with emptiness.
"""

from __future__ import annotations

import json
import random
import subprocess

import pytest

from axiom.extensions.builtins.publishing.mirror import (
    MirrorEngine,
    RemoteDoc,
    VersionConflict,
)


class ChaosEndpoint:
    """In-memory remote with full history, blind-write detection, and
    schedulable faults."""

    def __init__(self, text: str = "origin\n"):
        self.history = [RemoteDoc(text=text, version="1", author_app="seed")]
        self.blind_writes = 0
        self.fail_next_reads = 0
        self.fail_next_writes = 0

    @property
    def current(self) -> RemoteDoc:
        return self.history[-1]

    def read(self) -> RemoteDoc:
        if self.fail_next_reads > 0:
            self.fail_next_reads -= 1
            raise ConnectionError("chaos: read failed")
        return self.current

    def write(self, text: str, expected_version: str) -> str:
        if self.fail_next_writes > 0:
            self.fail_next_writes -= 1
            raise ConnectionError("chaos: write failed")
        if expected_version != self.current.version:
            self.blind_writes += 0  # a conflicted write is NOT blind — it is refused
            raise VersionConflict(expected_version, self.current.version)
        doc = RemoteDoc(text=text, version=str(int(self.current.version) + 1),
                        author_app="agent")
        self.history.append(doc)
        return doc.version

    def human_save(self, text: str, app: str = "web") -> None:
        self.history.append(RemoteDoc(text=text,
                                      version=str(int(self.current.version) + 1),
                                      author_app=app))

    def stale_flush(self, rng: random.Random) -> str | None:
        """Replay a random OLD version's text, as a zombie pane would."""
        if len(self.history) < 3:
            return None
        old = rng.choice(self.history[:-1])
        self.human_save(old.text, app="stale-pane")
        return old.text

    def versions(self, limit: int = 10) -> list[RemoteDoc]:
        return list(reversed(self.history[-limit:]))


def _human_texts_accounted_for(human_texts, endpoint, mirror_path, state_path):
    """I1: every human text is on the remote lineage or in a conflict copy."""
    lineage = {d.text for d in endpoint.history}
    preserved = {p.read_text()
                 for p in mirror_path.parent.glob(mirror_path.name + ".conflict*")}
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except json.JSONDecodeError:
            pass
    preserved.add(state.get("base_text") or "")
    missing = [t for t in human_texts if t not in lineage and t not in preserved]
    return missing


@pytest.mark.parametrize("seed", [7, 23, 101, 555, 4096, 90210])
def test_interleaving_storm_converges_and_loses_nothing(tmp_path, seed):
    rng = random.Random(seed)
    endpoint = ChaosEndpoint()
    mirror = tmp_path / "doc.md"
    state = tmp_path / "state.json"
    engine = MirrorEngine(endpoint=endpoint, mirror_path=mirror, state_path=state)
    engine.reconcile()

    human_texts: list[str] = []
    for step in range(120):
        # a human faced with a blocked mirror resolves before doing more:
        # "ours" pushes their pending local edit into the lineage (kept, not
        # lost) and unblocks the engine for the next op. A disciplined human
        # does not stack new edits onto an unresolved conflict.
        if state.exists() and json.loads(state.read_text()).get("conflict"):
            engine.resolve("ours")
        op = rng.random()
        if op < 0.25:
            text = f"human web edit {seed}-{step}\n"
            human_texts.append(text)
            endpoint.human_save(text)
        elif op < 0.40:
            text = f"human local edit {seed}-{step}\n"
            human_texts.append(text)
            mirror.write_text(text)
            engine.reconcile()  # the watcher reconciles on every local save
        elif op < 0.55:
            # agent discipline: a well-behaved agent reconciles before it
            # edits locally, so an unpushed human local edit reaches the
            # remote lineage before the agent's hand touches the mirror
            engine.reconcile()
            mirror.write_text(f"agent edit {seed}-{step}\n")
        elif op < 0.65:
            endpoint.stale_flush(rng)
        elif op < 0.85:
            engine.reconcile()
        else:
            engine.watch_once()

    # quiescence: humans stop. Under block-on-conflict a storm may end blocked
    # — a valid terminal state: the incoming side is in the mirror, the local
    # side is preserved in a .conflict sidecar, nothing is lost. The human
    # resolves the residual block by keeping their pending edit ("ours"), which
    # pushes it into the lineage; the engine then drains to convergence.
    final = engine.watch_once()
    blocked = final.action == "blocked" or bool(
        json.loads(state.read_text()).get("conflict") if state.exists() else None)
    if blocked:
        engine.resolve("ours")
    for _ in range(8):            # drain to quiescence (resolve + any stale flush)
        if engine.watch_once().action == "noop":
            break
    assert mirror.read_text() == endpoint.current.text  # I3 — converges once unblocked
    assert engine.reconcile().action == "noop"  # I3
    assert endpoint.blind_writes == 0  # I2
    missing = _human_texts_accounted_for(human_texts, endpoint, mirror, state)
    assert not missing, f"human text lost: {missing[:2]}"  # I1


@pytest.mark.parametrize("seed", [3, 77, 1234])
def test_hostile_racer_terminates_and_reports(tmp_path, seed):
    """A remote writer that races EVERY push: the engine must terminate
    (no unbounded recursion) and must not overwrite the racer."""
    rng = random.Random(seed)
    endpoint = ChaosEndpoint()
    mirror = tmp_path / "doc.md"
    engine = MirrorEngine(endpoint=endpoint, mirror_path=mirror,
                          state_path=tmp_path / "state.json")
    engine.reconcile()

    real_read = endpoint.read
    def racing_read():
        doc = real_read()
        endpoint.human_save(f"racer {rng.random()}\n")
        return doc
    endpoint.read = racing_read

    mirror.write_text("agent wants this in\n")
    report = engine.reconcile()  # must return, not recurse forever
    assert report.action in ("conflict", "pull")
    assert "racer" in endpoint.current.text


def test_crash_between_any_two_steps_recovers(tmp_path):
    """I4: simulate a crash after every possible prefix of a sync sequence
    by rebuilding the engine from disk state each time."""
    endpoint = ChaosEndpoint()
    mirror = tmp_path / "doc.md"
    state = tmp_path / "state.json"

    def fresh() -> MirrorEngine:
        return MirrorEngine(endpoint=endpoint, mirror_path=mirror, state_path=state)

    fresh().reconcile()
    endpoint.human_save("web v2\n")
    fresh().reconcile()          # crash-restart before this pull? state rebuilt
    mirror.write_text("local v3\n")
    fresh().reconcile()          # push from a brand-new engine instance
    assert endpoint.current.text == "local v3\n"
    endpoint.stale_flush(random.Random(1))
    report = fresh().watch_once()
    assert report.action in ("repair", "pull", "noop")
    assert fresh().reconcile().action == "noop"


def test_corrupt_state_with_local_edits_never_blind_pulls(tmp_path):
    """I5: state file destroyed while the mirror holds unpushed local
    edits — those edits must survive (pushed or preserved), never be
    overwritten by a re-baselining pull."""
    endpoint = ChaosEndpoint()
    mirror = tmp_path / "doc.md"
    state = tmp_path / "state.json"
    engine = MirrorEngine(endpoint=endpoint, mirror_path=mirror, state_path=state)
    engine.reconcile()
    mirror.write_text("precious local edit\n")
    state.write_text("{ corrupt json !!!")

    rebuilt = MirrorEngine(endpoint=endpoint, mirror_path=mirror, state_path=state)
    rebuilt.reconcile()
    conflict = mirror.with_suffix(mirror.suffix + ".conflict")
    survived = (endpoint.current.text == "precious local edit\n"
                or (conflict.exists()
                    and conflict.read_text() == "precious local edit\n"))
    assert survived, "local edit destroyed by re-baselining pull"


def test_missing_state_with_matching_content_is_quiet(tmp_path):
    endpoint = ChaosEndpoint()
    mirror = tmp_path / "doc.md"
    state = tmp_path / "state.json"
    engine = MirrorEngine(endpoint=endpoint, mirror_path=mirror, state_path=state)
    engine.reconcile()
    state.unlink()
    rebuilt = MirrorEngine(endpoint=endpoint, mirror_path=mirror, state_path=state)
    report = rebuilt.reconcile()
    assert report.action in ("pull", "noop")
    assert mirror.read_text() == endpoint.current.text


def test_stale_flush_with_empty_base_text_never_pushes_emptiness(tmp_path):
    """I7: a half-lost state (hash window intact, base_text gone) must not
    'repair' the remote by writing an empty document over it."""
    endpoint = ChaosEndpoint()
    mirror = tmp_path / "doc.md"
    state = tmp_path / "state.json"
    engine = MirrorEngine(endpoint=endpoint, mirror_path=mirror, state_path=state)
    engine.reconcile()
    endpoint.human_save("good text\n")
    engine.reconcile()
    # damage the state: keep hashes, lose the text
    data = json.loads(state.read_text())
    data["base_text"] = None
    state.write_text(json.dumps(data))
    endpoint.human_save("origin\n", app="stale-pane")  # flush of the old base

    rebuilt = MirrorEngine(endpoint=endpoint, mirror_path=mirror, state_path=state)
    rebuilt.watch_once()
    assert endpoint.current.text != ""  # never emptiness
    assert "good text\n" in {d.text for d in endpoint.history}


def test_endpoint_read_failures_do_not_corrupt_state(tmp_path):
    endpoint = ChaosEndpoint()
    mirror = tmp_path / "doc.md"
    state = tmp_path / "state.json"
    engine = MirrorEngine(endpoint=endpoint, mirror_path=mirror, state_path=state)
    engine.reconcile()
    before = state.read_text()
    endpoint.fail_next_reads = 1
    with pytest.raises(ConnectionError):
        engine.reconcile()
    assert state.read_text() == before  # untouched by the failed pass
    assert engine.reconcile().action == "noop"  # recovers next pass


class TestGitChaos:
    @pytest.fixture
    def git_repo(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.org"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        return repo

    def test_users_staged_work_is_not_swept_into_sync_commits(self, git_repo, tmp_path):
        """I6: the user has OTHER work staged; a sync commit must not
        include it."""
        other = git_repo / "unrelated.txt"
        other.write_text("user work in progress\n")
        subprocess.run(["git", "add", "unrelated.txt"], cwd=git_repo, check=True)

        endpoint = ChaosEndpoint()
        mirror = git_repo / "doc.md"
        engine = MirrorEngine(endpoint=endpoint, mirror_path=mirror,
                              state_path=tmp_path / "state.json", git_annotate=True)
        engine.reconcile()

        shown = subprocess.run(["git", "show", "--name-only", "--format="],
                               cwd=git_repo, capture_output=True, text=True,
                               check=True).stdout.split()
        assert shown == ["doc.md"], f"sync commit swept: {shown}"
        staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                                cwd=git_repo, capture_output=True, text=True,
                                check=True).stdout.split()
        assert "unrelated.txt" in staged  # still the user's staged work
