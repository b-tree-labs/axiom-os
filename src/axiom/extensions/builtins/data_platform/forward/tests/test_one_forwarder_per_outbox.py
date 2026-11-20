# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One forwarder sends from an outbox at a time, so a blue/green switch never runs two.

A blue/green edge (ADR-170) runs its companions in every slot it lists, on the
one data volume the slots share, and the old slot stays up after a switch for
rollback. The forwarder is such a companion. Two of them on one outbox raced on
one cursor and both used one Box login, whose refresh token rotates on every
use, so each revoked the other's copy and Box sending stopped with "login
expired" on a node that had done nothing wrong.

Now the forwarder holds an exclusive lock beside its cursor. Proven with a real
second process holding it: a single pass refuses at once and names the holder;
a watching forwarder waits and takes over the moment the holder exits.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from axiom.infra.skills import SkillContext, SkillRegistry

from ...skills import forward

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the lock is flock; slots share a Linux volume")

HOLDER = """
import fcntl, os, sys, time
from pathlib import Path
work = Path(sys.argv[1]); work.mkdir(parents=True, exist_ok=True)
f = open(work / "forward.lock", "a+")
fcntl.flock(f.fileno(), fcntl.LOCK_EX)
f.seek(0); f.truncate(); f.write(str(os.getpid())); f.flush()
print("held", flush=True)
while not (work / "release").exists():
    time.sleep(0.05)
"""


@pytest.fixture
def holder(tmp_path):
    work = tmp_path / "state" / "forward"
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(work)], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    yield work, proc
    (work / "release").touch()
    proc.wait(timeout=10)


def _ctx(tmp_path: Path) -> SkillContext:
    return SkillContext(registry=SkillRegistry(), state_dir=tmp_path / "state", logger=logging.getLogger("t"))


def test_a_single_pass_refuses_while_another_forwarder_sends_and_names_it(tmp_path, holder):
    work, proc = holder
    (tmp_path / "outbox").mkdir()
    r = forward.run({"outbox_dir": str(tmp_path / "outbox"), "box_remote": "drop:folder"}, _ctx(tmp_path))
    assert not r.ok
    assert f"pid {proc.pid}" in r.errors[0] and "sent nothing" in r.errors[0]


def test_a_watching_forwarder_waits_then_takes_over_when_the_holder_stops(holder):
    work, proc = holder
    waited: list[str] = []
    got: dict = {}

    def second():
        got["lock"], got["who"] = forward.single_flight(work, wait=True, on_wait=waited.append)

    t = threading.Thread(target=second, daemon=True)
    t.start()
    time.sleep(0.5)
    assert t.is_alive(), "the second forwarder must not start while the first holds the outbox"
    assert waited == [str(proc.pid)]
    (work / "release").touch()
    proc.wait(timeout=10)
    t.join(timeout=10)
    assert not t.is_alive() and got["lock"] is not None
    assert (work / forward.LOCK_NAME).read_text().strip() == str(os.getpid())
    got["lock"].close()


def test_with_nobody_else_the_lock_is_taken_at_once_and_released_on_close(tmp_path):
    work = tmp_path / "forward"
    lock, who = forward.single_flight(work, wait=False)
    assert lock is not None and who == ""
    again, holder_pid = forward.single_flight(work, wait=False)
    assert again is None and holder_pid == str(os.getpid())
    lock.close()
    third, _ = forward.single_flight(work, wait=False)
    assert third is not None
    third.close()
