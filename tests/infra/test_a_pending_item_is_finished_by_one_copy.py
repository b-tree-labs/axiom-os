# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""A pending item is finished by the holder of its claim, and only by it.

Two copies of a service overlap during a zero-gap switch; both run their
"resume pending work" step. The claim makes the second one stand aside while the
first is alive, and take over once it has gone.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap

from axiom.infra import pending_claim as pc
from axiom.infra import process_registry


def _item(tmp_path, name="q1.json"):
    p = tmp_path / name
    p.write_text(json.dumps({"question": "?"}))
    return p


def test_a_second_claim_on_a_held_item_is_refused(tmp_path):
    item = _item(tmp_path)
    first = pc.claim(item)
    assert first is not None and first.held()
    assert pc.claim(item) is None, "even the same process may not finish it twice"
    first.release()
    assert not pc.claim_path(item).exists()
    again = pc.claim(item)
    assert again is not None, "a released item can be claimed again"
    again.release()


def test_release_only_drops_a_claim_that_is_still_ours(tmp_path):
    item = _item(tmp_path)
    first = pc.claim(item)
    pc.claim_path(item).write_text(json.dumps({"pid": os.getpid(), "token": "someone-else"}))
    first.release()
    assert pc.claim_path(item).exists(), "a taker's claim is not released by the old holder"


def test_the_claim_is_released_on_exit_from_the_context(tmp_path):
    item = _item(tmp_path)
    with pc.claim(item) as c:
        assert c.held()
    assert not pc.claim_path(item).exists()


RACER = textwrap.dedent('''
    import sys, time, pathlib
    from axiom.infra import pending_claim as pc
    item, go, out = sys.argv[1], pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
    while not go.exists():
        time.sleep(0.005)
    c = pc.claim(item)
    if c is not None:
        with open(out, "a") as f:
            f.write("finished\\n")
        time.sleep(0.5)   # in flight while the others have already tried
        c.release()
''')


def test_two_copies_racing_on_one_item_finish_it_once(tmp_path):
    item = _item(tmp_path)
    go, out = tmp_path / "go", tmp_path / "finished.log"
    procs = [subprocess.Popen([sys.executable, "-c", RACER, str(item), str(go), str(out)]) for _ in range(4)]
    go.write_text("go")
    for p in procs:
        assert p.wait(timeout=60) == 0
    assert out.read_text().count("finished") == 1


def _dead_claim(item):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    info = None
    while info is None:
        info = process_registry.probe(p.pid)
    pc.claim_path(item).write_text(json.dumps({"pid": p.pid, "started_at": info.start, "token": "old"}))
    return p


def test_a_claim_held_by_a_live_other_copy_is_left_alone(tmp_path):
    item = _item(tmp_path)
    old = _dead_claim(item)
    try:
        assert pc.claim(item) is None
        assert list(pc.claimable(tmp_path)) == [], "the new copy's resume skips the old copy's in-flight item"
        assert not pc.is_stale(item)
    finally:
        old.kill()
        old.wait()


def test_a_stale_claim_is_taken_over_once_its_owner_has_exited(tmp_path):
    item = _item(tmp_path)
    old = _dead_claim(item)
    os.kill(old.pid, signal.SIGKILL)
    old.wait()
    assert pc.is_stale(item)
    taken = list(pc.claimable(tmp_path))
    assert [c.item for c in taken] == [item]
    assert taken[0].held()
    assert pc.claim(item) is None, "and the takeover is itself a claim"
    taken[0].release()


def test_a_reused_pid_does_not_keep_a_claim_alive(tmp_path):
    item = _item(tmp_path)
    pc.claim_path(item).write_text(json.dumps({"pid": os.getpid(), "started_at": 1.0, "token": "old"}))
    assert pc.is_stale(item)


def test_a_silent_owner_is_stale_after_the_stated_time(tmp_path):
    item = _item(tmp_path)
    c = pc.claim(item)
    assert not pc.is_stale(item, stale_after_s=60)
    old = os.stat(c.path).st_mtime - 120
    os.utime(c.path, (old, old))
    assert pc.is_stale(item, stale_after_s=60)
    c.touch()
    assert not pc.is_stale(item, stale_after_s=60), "a heartbeat keeps a long job's claim fresh"
    c.release()


def test_claimable_skips_claim_files_and_vanished_items(tmp_path):
    a, b = _item(tmp_path, "a.json"), _item(tmp_path, "b.json")
    held = pc.claim(a)
    got = list(pc.claimable(tmp_path))
    assert [c.item for c in got] == [b]
    for c in got + [held]:
        c.release()
