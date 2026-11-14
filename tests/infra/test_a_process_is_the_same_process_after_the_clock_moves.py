# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A process is still the same process after the wall clock is corrected.

Claims (``pending_claim``) and process records (``process_registry``) tell a live
owner from a reused pid by the process's start time. On Linux, ``ps`` reports that
start as boot time plus "ticks since boot", and works the boot time out as *now*
minus the uptime, so correcting the wall clock moves every process's reported
start. On a WSL2 node a capture ran for 26 minutes, the clock was corrected by 32 s,
and its own claim then read as abandoned: ``status`` said nothing was capturing,
``stop`` said there was nothing to stop, and a second ``start`` would have taken the
source from the copy still holding it.

The fix: a process's start is also recorded as ``<boot id>:<ticks since boot>``
(``start_id``), which the kernel fixes at fork and no clock change touches, and two
starts are compared by that when both sides have it. A claim or record written
before this change has no ``start_id`` and is still judged by the wall start within
the tolerance, so an old and a new copy can share one during a switch (ADR-182).
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from axiom.infra import pending_claim as pc
from axiom.infra import process_containment as pcon
from axiom.infra import process_registry as pr

LINUX = Path("/proc/self/stat").exists()
needs_proc = pytest.mark.skipif(not LINUX, reason="start_id comes from /proc (Linux)")

#: What the WSL2 node's clock was corrected by during the run that found this.
JUMP_S = 32.0


@pytest.fixture
def clock_jump(monkeypatch):
    """``jump()``: from then on every start ``ps`` reports is ``JUMP_S`` later, as after a
    correction. Call it once the claim or record exists, as it happened on the node."""
    real = pr._parse_ps_line

    def shifted(line: str):
        info = real(line)
        return None if info is None else dataclasses.replace(info, start=info.start + JUMP_S)

    def jump() -> None:
        monkeypatch.setattr(pr, "_parse_ps_line", shifted)

    return jump


def _item(tmp_path: Path, name: str = "q.json") -> Path:
    p = tmp_path / name
    p.write_text("{}")
    return p


@pytest.fixture
def child():
    # Its own session, so stopping it never touches the test's process group.
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
    for _ in range(100):
        if pr.probe(p.pid):
            break
        time.sleep(0.05)
    yield p
    p.kill()
    p.wait()


# ---- the identity itself -------------------------------------------------------------


@needs_proc
def test_a_live_process_has_a_start_id_that_does_not_change():
    first = pr.probe(os.getpid())
    again = pr.probe(os.getpid())
    assert first is not None and first.start_id
    assert first.start_id == again.start_id
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    head, _, ticks = first.start_id.rpartition(":")
    assert head == boot_id and ticks.isdigit()


@needs_proc
def test_two_processes_have_different_start_ids(child):
    assert pr.probe(child.pid).start_id != pr.probe(os.getpid()).start_id


@needs_proc
def test_list_procs_carries_the_same_start_id_as_probe():
    me = os.getpid()
    assert pr.list_procs()[me].start_id == pr.probe(me).start_id


@needs_proc
def test_the_start_id_does_not_move_when_the_wall_clock_does(clock_jump):
    """The jump moves what ``ps`` says; the start id comes from the kernel's ticks."""
    before = pr.probe(os.getpid())
    clock_jump()
    after = pr.probe(os.getpid())
    assert after.start == before.start + JUMP_S, "the fixture moved the wall start"
    assert after.start_id == before.start_id


def test_the_ticks_are_read_past_a_name_with_spaces_and_parentheses():
    # /proc/<pid>/stat is "pid (comm) state ppid ..."; comm may hold ") (" itself.
    fields = ["S"] + [str(n) for n in range(4, 22)] + ["987654"] + ["0"] * 30
    line = "4242 (a) b (c)) " + " ".join(fields)
    assert pr._ticks_from_stat(line) == 987654


def test_a_missing_or_unreadable_stat_gives_no_start_id(tmp_path):
    assert pr._ticks_from_stat("") is None
    assert pr._ticks_from_stat("garbage without the name") is None


# ---- claims ------------------------------------------------------------------------


@needs_proc
def test_a_live_claim_holder_is_alive_after_the_clock_jumps(tmp_path, clock_jump):
    item = _item(tmp_path)
    held = pc.claim(item)
    assert held is not None
    clock_jump()
    try:
        assert not pc.is_stale(item), "the holder is this very process"
        assert pc.claim(item) is None, "and nobody may take its item"
    finally:
        held.release()


@needs_proc
def test_a_live_claim_written_before_the_jump_records_the_start_id(tmp_path):
    item = _item(tmp_path)
    held = pc.claim(item)
    try:
        data = json.loads(pc.claim_path(item).read_text())
        assert data["start_id"] == pr.probe(os.getpid()).start_id
        assert data["started_at"], "the wall start is still written, for older readers"
    finally:
        held.release()


@needs_proc
def test_a_reused_pid_is_another_process_even_with_the_same_wall_start(tmp_path):
    item = _item(tmp_path)
    me = pr.probe(os.getpid())
    pc.claim_path(item).write_text(
        json.dumps(
            {"pid": os.getpid(), "started_at": me.start, "start_id": "another-boot:1", "token": "t"}
        )
    )
    assert pc.is_stale(item)


def test_a_claim_written_before_this_change_is_judged_by_its_wall_start(tmp_path, child):
    """No ``start_id``: what an older copy writes during a switch. Same rules as before."""
    item = _item(tmp_path)
    live = pr.probe(child.pid)
    pc.claim_path(item).write_text(
        json.dumps({"pid": child.pid, "started_at": live.start, "token": "old"})
    )
    assert not pc.is_stale(item)
    pc.claim_path(item).write_text(
        json.dumps({"pid": child.pid, "started_at": live.start - 3600, "token": "old"})
    )
    assert pc.is_stale(item), "a wall start an hour off is a reused pid"


@needs_proc
def test_a_dead_holder_is_still_stale_after_the_clock_jumps(tmp_path, clock_jump):
    item = _item(tmp_path)
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        info = None
        while info is None:
            info = pr.probe(p.pid)
        pc.claim_path(item).write_text(
            json.dumps(
                {"pid": p.pid, "started_at": info.start, "start_id": info.start_id, "token": "o"}
            )
        )
        clock_jump()
        assert not pc.is_stale(item)
    finally:
        p.kill()
        p.wait()
    assert pc.is_stale(item)


# ---- process records and containment ---------------------------------------------------


@pytest.fixture
def registry(tmp_path):
    return pr.ProcessRegistry(tmp_path / "state", scope="t" + uuid.uuid4().hex[:8])


@needs_proc
def test_a_record_carries_the_start_ids_of_the_process_and_its_parent(registry, child):
    rec = registry.register(child.pid, kind="tool", purpose="t")
    assert rec.start_id == pr.probe(child.pid).start_id
    assert rec.parent_start_id == pr.probe(os.getpid()).start_id


@needs_proc
def test_a_registered_process_is_still_ours_after_the_clock_jumps(registry, child, clock_jump):
    rec = registry.register(child.pid, kind="tool", purpose="t")
    clock_jump()
    ok, reason, _ = pr.verify_ours(rec)
    assert ok, reason


@needs_proc
def test_detection_sees_no_reused_pid_or_orphan_after_the_clock_jumps(registry, child, clock_jump):
    registry.register(child.pid, kind="tool", purpose="t")
    clock_jump()
    kinds = [f.kind for f in pcon.detect_rogues(registry, scan_escaped=False).findings]
    assert "pid_reused" not in kinds and "orphan" not in kinds, kinds


@needs_proc
def test_our_process_can_still_be_stopped_after_the_clock_jumps(registry, child, clock_jump):
    rec = registry.register(child.pid, kind="tool", purpose="t")
    clock_jump()
    res = pcon.terminate(rec, state_dir=registry.state_dir, why="t", execute=True, grace_s=0.5)
    assert res["signalled"] is True, res
    child.wait(timeout=10)


def test_a_record_written_before_this_change_still_loads_and_verifies(registry, child):
    rec = registry.register(child.pid, kind="tool", purpose="t")
    data = rec.to_dict()
    data.pop("start_id", None)
    data.pop("parent_start_id", None)
    (registry.dir / f"{rec.id}.json").write_text(json.dumps(data))
    (loaded,) = registry.records()
    assert loaded.start_id == "" and pr.verify_ours(loaded)[0]
