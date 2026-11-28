# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What happens to a source when the process holding it dies, or is replaced (VID-007, ADR-182).

Real processes, stand-in ``ffmpeg``:

* a capture killed with ``SIGKILL`` leaves no decoder behind (its pipe closes,
  the decoder's next write fails), status says nothing is capturing a started
  source and how to bring it back, and ``start`` takes over the dead claim;
* a second copy started beside the first (a deploy) waits on the claim, is shown
  as waiting, and takes the source when the first copy is terminated, without
  the source ever being stopped;
* after ``stop`` no decoder for the source is left running.
"""

from __future__ import annotations

import os
import signal

import pytest

from axiom.extensions.builtins.video import state as vs

from .harness import alive, decoder_pids, envelope, run_cli, spawn_all_sources, wait_for

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="the stand-in tools are POSIX shell wrappers"
)


def _status(state_dir) -> dict:
    return envelope(run_cli(["status", "cam-1", "--json"], state_dir=state_dir))["value"][
        "sources"
    ][0]


def _running(state_dir, *, pid: int | None = None, after: int = 0):
    def check():
        row = _status(state_dir)
        if row["state"] != "running" or row["frames"] <= after:
            return None
        return row if pid is None or row["pid"] == pid else None

    return check


@pytest.fixture
def started(state_dir, fake_ffmpeg, no_leftovers) -> dict:
    added = run_cli(
        ["add", "cam-1", "--kind", "rtsp", "--url", "rtsp://cam.example.org/ok", "--json"],
        state_dir=state_dir,
    )
    assert added.returncode == 0, added.stdout + added.stderr
    begun = envelope(run_cli(["start", "cam-1", "--json"], state_dir=state_dir))
    assert begun["ok"], begun
    row = wait_for(_running(state_dir), timeout=30, every=0.3)
    assert row, "capture never reached running"
    return row


def test_a_killed_capture_leaves_no_decoder_and_start_takes_it_back(state_dir, started):
    pid = started["pid"]
    decoders = wait_for(lambda: decoder_pids(state_dir, "cam-1"), timeout=10)
    assert decoders, "the registry names the decoder"

    os.kill(pid, signal.SIGKILL)

    assert wait_for(lambda: not alive(pid), timeout=10)
    assert wait_for(lambda: not any(alive(d) for d in decoders), timeout=15), (
        "the orphaned decoder exited"
    )
    row = wait_for(lambda: (r := _status(state_dir))["state"] == "stopped" and r, timeout=10)
    assert row and row["started"] is True and row["health"] == "degraded"
    assert "start" in row["note"]

    again = envelope(run_cli(["start", "cam-1", "--json"], state_dir=state_dir))
    assert again["ok"] and again["value"]["already_running"] is False
    assert again["value"]["pid"] != pid
    assert wait_for(_running(state_dir, pid=again["value"]["pid"]), timeout=30, every=0.3)


def test_a_second_copy_waits_then_takes_over_when_the_first_is_terminated(state_dir, started):
    first = started["pid"]
    first_session = started["session_id"]
    second = spawn_all_sources(state_dir)
    try:
        waiting = wait_for(
            lambda: (r := _status(state_dir))["standby"] and r, timeout=20, every=0.3
        )
        assert waiting, "the second copy is shown waiting"
        assert waiting["pid"] == first and [s["pid"] for s in waiting["standby"]] == [second.pid]
        assert decoder_pids(state_dir, "cam-1") and len(decoder_pids(state_dir, "cam-1")) == 1

        os.kill(first, signal.SIGTERM)  # what a switch does to the old copy

        row = wait_for(_running(state_dir, pid=second.pid), timeout=30, every=0.3)
        assert row, "the new copy holds the source"
        assert row["session_id"] != first_session and row["standby"] == []
        assert vs.is_started("cam-1", state_dir), "a switch is not a stop"
        assert wait_for(lambda: len(decoder_pids(state_dir, "cam-1")) == 1, timeout=10)

        stopped = envelope(run_cli(["stop", "cam-1", "--json"], state_dir=state_dir))
        assert stopped["value"]["stopped"] is True and stopped["value"]["signalled"] is False
        assert second.poll() is None, "a service capturing every started source keeps running"
    finally:
        second.terminate()
        second.wait(timeout=15)


def test_after_stop_no_decoder_is_left(state_dir, started):
    decoders = wait_for(lambda: decoder_pids(state_dir, "cam-1"), timeout=10)
    assert envelope(run_cli(["stop", "cam-1", "--json"], state_dir=state_dir))["value"]["stopped"]
    assert wait_for(lambda: not any(alive(d) for d in decoders), timeout=10)
    # stop returns once the claim is released; the process exits a moment after.
    assert wait_for(lambda: not alive(started["pid"]), timeout=10)
