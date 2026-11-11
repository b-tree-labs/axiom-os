# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Two test sweeps never run at once on one machine, and a dead holder frees the lock."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

HOOK = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "pre-push"


def _lock_code() -> str:
    text = HOOK.read_text(encoding="utf-8")
    return re.search(r"HOOK_LOCK_DIR=.*?# end hook lock", text, re.S).group(0)


def _run(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "AXI_HOOK_LOCK_DIR": str(tmp_path / "locks"),
        "AXI_HOOK_LOCK_WAIT": "1",
        "AXI_HOOK_LOCK_POLL": "1",
    }
    return subprocess.run(
        ["bash", "-c", _lock_code() + "\n" + body],
        env=env, capture_output=True, text=True, timeout=30,
    )


def _hold(tmp_path: Path, pid: int) -> Path:
    lock = tmp_path / "locks" / "pre-push-sweep"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(pid), encoding="utf-8")
    return lock


def test_a_free_lock_is_taken_and_given_back(tmp_path):
    r = _run(tmp_path, "hook_lock_acquire && hook_lock_release && echo done")
    assert "done" in r.stdout
    assert not (tmp_path / "locks" / "pre-push-sweep").exists()


def test_a_live_holder_makes_the_second_sweep_wait_then_refuse(tmp_path):
    with subprocess.Popen(["sleep", "20"]) as holder:
        lock = _hold(tmp_path, holder.pid)
        r = _run(tmp_path, "hook_lock_acquire")
        holder.kill()
    assert r.returncode == 1
    assert "another test sweep" in r.stdout
    assert (lock / "pid").read_text(encoding="utf-8") == str(holder.pid)


def test_a_dead_holder_does_not_block_anyone(tmp_path):
    p = subprocess.Popen(["true"])
    p.wait()
    _hold(tmp_path, p.pid)
    r = _run(tmp_path, "hook_lock_acquire && echo taken")
    assert "taken" in r.stdout


def test_a_small_selection_does_not_queue_behind_a_sweep():
    text = HOOK.read_text(encoding="utf-8")
    assert 'if [ "$_mode" = "blast" ] && [ "${_workers:-0}" -eq 0 ]; then _sweep=0; fi' in text
