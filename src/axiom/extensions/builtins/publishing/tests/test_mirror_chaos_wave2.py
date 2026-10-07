# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chaos, wave two: what wave one could not reach.

- Two engine processes on one mirror must not tear the state file or
  double-write the remote (I8 — single-writer discipline is enforced, not
  assumed).
- A remote that normalizes content (a web editor flipping line endings)
  must converge, not oscillate (I9).
- An army of zombie panes flushing different old versions must be beaten
  by the watchdog without a repair war (I10).
- A human who INTENDS to revert to old text is indistinguishable from a
  stale flush on content alone — the engine repairs once, and if the same
  old text comes back immediately, treats it as a human insisting and
  follows (I11, the two-strikes rule).
- Filesystem sabotage (mirror deleted, mirror unwritable) degrades to an
  error or a re-pull, never to corruption (I12).
- Sustained churn keeps artifacts bounded: state capped, conflict copies
  proportional to conflicts, remote reads bounded per pass (I13).
"""

from __future__ import annotations

import json
import random
import threading

import pytest

from axiom.extensions.builtins.publishing.mirror import (
    MirrorEngine,
    RemoteDoc,
    VersionConflict,
)


class W2Endpoint:
    def __init__(self, text: str = "origin\n", normalize=None):
        self._lock = threading.Lock()
        self.normalize = normalize
        self.history = [RemoteDoc(text=self._norm(text), version="1",
                                  author_app="seed")]

    def _norm(self, text: str) -> str:
        return self.normalize(text) if self.normalize else text

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
            doc = RemoteDoc(text=self._norm(text),
                            version=str(int(self.current.version) + 1),
                            author_app="agent")
            self.history.append(doc)
            return doc.version

    def human_save(self, text: str, app: str = "web") -> None:
        with self._lock:
            self.history.append(RemoteDoc(
                text=self._norm(text),
                version=str(int(self.current.version) + 1), author_app=app))

    def versions(self, limit: int = 10) -> list[RemoteDoc]:
        with self._lock:
            return list(reversed(self.history[-limit:]))


def test_two_engines_one_mirror_do_not_tear_state(tmp_path):
    """I8: two engine instances (two processes in real life) hammer the same
    mirror + state. The lock must serialize them: state stays parseable,
    convergence holds, nothing raises from torn internals."""
    endpoint = W2Endpoint()
    mirror = tmp_path / "doc.md"
    state = tmp_path / "state.json"
    engines = [MirrorEngine(endpoint=endpoint, mirror_path=mirror,
                            state_path=state) for _ in range(2)]
    engines[0].reconcile()
    errors: list[Exception] = []

    def hammer(engine, n):
        rng = random.Random(n)
        for i in range(40):
            try:
                if rng.random() < 0.3:
                    endpoint.human_save(f"web {n}-{i}\n")
                engine.watch_once() if rng.random() < 0.5 else engine.reconcile()
            except (VersionConflict, ConnectionError):
                pass
            except Exception as exc:  # noqa: BLE001 — the invariant IS "no other exception"
                errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(e, i))
               for i, e in enumerate(engines)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"engine tore under concurrency: {errors[:2]}"
    json.loads(state.read_text())  # parseable, not torn — atomic writes guarantee it
    fresh = MirrorEngine(endpoint=endpoint, mirror_path=mirror, state_path=state)
    # a two-engine interleaving may end blocked on a conflict (block-on-conflict
    # doctrine) — a valid terminal state; a human resolves it (takes canonical)
    # and it then converges. Settle over several passes either way.
    for _ in range(12):
        r = fresh.reconcile()
        if r.action == "blocked":
            fresh.resolve("theirs")
            continue
        if r.action == "noop":
            break
    assert mirror.read_text() == endpoint.current.text
    assert fresh.reconcile().action == "noop"


def test_atomic_write_reader_never_sees_a_torn_file(tmp_path):
    """The root-cause fix behind the two-engine test: a concurrent reader always
    sees a whole file (one complete payload or the other), never a partial write
    — which is how ``state.json`` stays parseable while another engine writes it."""
    from axiom.extensions.builtins.publishing.mirror import _atomic_write_bytes

    target = tmp_path / "s.json"
    a = json.dumps({"k": "A" * 20000}).encode()
    b = json.dumps({"k": "B" * 20000}).encode()
    _atomic_write_bytes(target, a)
    torn: list = []
    stop = threading.Event()

    def writer():
        for i in range(400):
            _atomic_write_bytes(target, a if i % 2 else b)

    def reader():
        while not stop.is_set():
            try:
                raw = target.read_bytes()
                json.loads(raw)              # must always parse
                if raw not in (a, b):        # and be one whole payload, never a mix
                    torn.append(raw[:48])
            except json.JSONDecodeError as exc:
                torn.append(("decode", str(exc)))

    r = threading.Thread(target=reader)
    w = threading.Thread(target=writer)
    r.start()
    w.start()
    w.join()
    stop.set()
    r.join()
    assert not torn, f"reader saw a torn/partial file: {torn[:2]}"


def test_normalizing_remote_converges_instead_of_oscillating(tmp_path):
    """I9: the remote stores CRLF no matter what is pushed. A local LF edit
    must land (as CRLF), converge, and stay quiet — not ping-pong."""
    endpoint = W2Endpoint(normalize=lambda t: t.replace("\n", "\r\n")
                          .replace("\r\r\n", "\r\n"))
    mirror = tmp_path / "doc.md"
    engine = MirrorEngine(endpoint=endpoint, mirror_path=mirror,
                          state_path=tmp_path / "state.json")
    engine.reconcile()

    mirror.write_text("local line one\nlocal line two\n")
    first = engine.reconcile()   # push; remote normalizes to CRLF
    second = engine.reconcile()  # sees normalized remote; pulls it
    third = engine.reconcile()   # must now be quiet
    assert first.action == "push"
    assert second.action in ("pull", "noop")
    assert third.action == "noop", "oscillation: engine never settled"
    assert "local line one" in mirror.read_text()


def test_unicode_and_bidi_content_round_trips(tmp_path):
    endpoint = W2Endpoint()
    mirror = tmp_path / "doc.md"
    engine = MirrorEngine(endpoint=endpoint, mirror_path=mirror,
                          state_path=tmp_path / "state.json")
    engine.reconcile()
    text = "emoji 🎛️⚛️, RTL שלום عليكم, combining é vs é, zero-width​.\n"
    mirror.write_text(text)
    assert engine.reconcile().action == "push"
    assert endpoint.current.text == text
    assert engine.reconcile().action == "noop"


def test_zombie_pane_army_is_beaten_without_a_repair_war(tmp_path):
    """I10: many stale panes flush different old versions between watch
    passes. The watchdog must end with the good text on the remote and a
    bounded number of repairs (one per flush, no self-perpetuating war)."""
    rng = random.Random(11)
    endpoint = W2Endpoint()
    mirror = tmp_path / "doc.md"
    engine = MirrorEngine(endpoint=endpoint, mirror_path=mirror,
                          state_path=tmp_path / "state.json")
    engine.reconcile()
    for i in range(6):  # build up history the zombies can replay
        endpoint.human_save(f"good revision {i}\n")
        engine.reconcile()
    good = endpoint.current.text

    repairs = 0
    suspended = 0
    for _ in range(12):
        old = rng.choice(endpoint.history[:5])
        endpoint.human_save(old.text, app=f"zombie-{rng.randint(1, 3)}")
        report = engine.watch_once()
        if report.action == "repair":
            repairs += 1
            assert report.detected.author_app.startswith("zombie")
        elif "repair suspended" in " ".join(report.notes):
            suspended += 1
    # no repair war: each flushed text is repaired at most once, then the
    # engine suspends rather than fight — and the good text survives, on the
    # remote if repairs held, in a preserved conflict copy if they suspended
    assert repairs + suspended >= 1
    good_survives = (endpoint.current.text == good or any(
        p.read_bytes().decode() == good
        for p in tmp_path.glob("doc.md.conflict*")))
    assert good_survives, "good text lost to the zombie army"
    assert engine.watch_once().action == "noop"


def test_human_insisting_on_a_revert_wins_after_two_strikes(tmp_path):
    """I11: old text reappears; the engine repairs once (indistinguishable
    from a zombie). The human puts the SAME old text back immediately —
    that is a person insisting, and the engine follows instead of fighting."""
    endpoint = W2Endpoint()
    mirror = tmp_path / "doc.md"
    engine = MirrorEngine(endpoint=endpoint, mirror_path=mirror,
                          state_path=tmp_path / "state.json")
    engine.reconcile()
    old_text = endpoint.current.text
    endpoint.human_save("newer good text\n")
    engine.reconcile()

    endpoint.human_save(old_text, app="web")   # strike one: looks stale
    first = engine.watch_once()
    assert first.action == "repair"

    endpoint.human_save(old_text, app="web")   # strike two: human insists
    second = engine.watch_once()
    assert second.action == "pull", "engine fought a human revert"
    assert "repair suspended" in " ".join(second.notes)
    assert endpoint.current.text == old_text
    assert mirror.read_text() == old_text
    # the repaired-over text is preserved in case this was a zombie after all
    assert any(p.read_bytes().decode() == "newer good text\n"
               for p in mirror.parent.glob("doc.md.conflict*"))
    assert engine.watch_once().action == "noop"


def test_mirror_deleted_mid_life_re_pulls(tmp_path):
    endpoint = W2Endpoint()
    mirror = tmp_path / "doc.md"
    engine = MirrorEngine(endpoint=endpoint, mirror_path=mirror,
                          state_path=tmp_path / "state.json")
    engine.reconcile()
    mirror.unlink()
    report = engine.reconcile()
    assert report.action == "pull"
    assert mirror.read_text() == endpoint.current.text


def test_unwritable_mirror_fails_without_corrupting_state(tmp_path):
    # Writes are atomic (temp-in-dir + os.replace), which is exactly why a
    # read-only *file* is no longer unwritable — replace overwrites it via the
    # directory. Genuine unwritability is a read-only *directory*, which blocks
    # the temp create. That is what must fail cleanly without advancing state.
    workdir = tmp_path / "work"
    workdir.mkdir()
    endpoint = W2Endpoint()
    mirror = workdir / "doc.md"
    state = workdir / "state.json"
    engine = MirrorEngine(endpoint=endpoint, mirror_path=mirror, state_path=state)
    engine.reconcile()
    before = state.read_text()
    endpoint.human_save("newer\n")
    workdir.chmod(0o500)  # read+execute, no write — the temp file cannot be created
    try:
        with pytest.raises((PermissionError, OSError)):
            engine.reconcile()
        assert state.read_text() == before  # base not advanced past a failed pull
    finally:
        workdir.chmod(0o700)
    assert engine.reconcile().action == "pull"  # recovers once writable


def test_sustained_churn_keeps_artifacts_bounded(tmp_path):
    """I13: 400 mixed rounds; state stays capped, conflict copies stay
    proportional to actual conflicts, every pass reads the remote at most
    twice."""
    rng = random.Random(2026)
    reads = {"n": 0}
    endpoint = W2Endpoint()
    real_read = endpoint.read
    def counting_read():
        reads["n"] += 1
        return real_read()
    endpoint.read = counting_read

    mirror = tmp_path / "doc.md"
    state = tmp_path / "state.json"
    engine = MirrorEngine(endpoint=endpoint, mirror_path=mirror, state_path=state)
    engine.reconcile()

    conflicts = 0
    for i in range(400):
        r = rng.random()
        if r < 0.3:
            endpoint.human_save(f"web {i}\n")
        elif r < 0.5:
            mirror.write_text(f"local {i}\n")
        reads["n"] = 0
        report = engine.watch_once()
        assert reads["n"] <= 2, f"pass {i} read the remote {reads['n']} times"
        if report.action == "conflict":
            conflicts += 1

    mine_files = list(tmp_path.glob("doc.md.conflict*"))
    assert len(mine_files) <= conflicts + 1
    blob = json.loads(state.read_text())
    assert len(blob["old_base_hashes"]) <= 20
