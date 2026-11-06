# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A signal that arrives while the first copy is still starting is not lost (ADR-182).

The supervisor waits for its first copy to say it is ready before it serves. Until a
handler was installed, a SIGHUP in that window terminated the supervisor outright (a
reload sent four seconds after launch killed it on a loaded CI runner: no change
record, a non-zero exit) and a SIGTERM ended it without draining, leaving the copy
behind. The flags are now installed before the first copy starts: a reload is
remembered and applied once the copy is serving, and a stop is honoured as soon as it
is. Real processes.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time

SLOW_START = textwrap.dedent(
    """
    import os, signal, sys, time
    stop = False
    def on_term(*_):
        global stop
        stop = True
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGUSR1, lambda *_: None)
    time.sleep(float(os.environ["STARTUP_S"]))      # a slow start: imports, migrations, a loaded runner
    fd = int(os.environ.pop("AXI_READY_FD"))
    os.write(fd, b"R"); os.close(fd)
    while not stop:
        time.sleep(0.1)
    """
)


def _launch(tmp_path, startup_s, state):
    script = tmp_path / "slow.py"
    script.write_text(SLOW_START)
    env = {**os.environ, "STARTUP_S": str(startup_s), "AXI_STATE_DIR": str(state)}
    return subprocess.Popen(
        [sys.executable, "-m", "axiom.infra.switch", "--worker", "--drain-s", "5",
         "--subject", "slow", "--", sys.executable, str(script)],
        env=env,
        start_new_session=True,
    )


def _reap(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if proc.poll() is None:
        proc.wait()


def _changes(state):
    from axiom.infra.change_intent import changes

    os.environ["AXI_STATE_DIR"] = str(state)
    return [(c.kind, c.subject, c.outcome) for c in changes() if c.kind == "deploy"]


def test_a_reload_during_startup_is_remembered_not_fatal(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    proc = _launch(tmp_path, startup_s=3.0, state=state)
    try:
        time.sleep(1.0)                       # the first copy is still starting
        proc.send_signal(signal.SIGHUP)
        time.sleep(0.5)
        assert proc.poll() is None, f"the supervisor died of a SIGHUP during startup (code {proc.poll()})"
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and not any(o for _, _, o in _changes(state)):
            time.sleep(0.25)  # the record opens when the switch starts and closes when it ends
        assert _changes(state) == [("deploy", "slow reload", "switched")], _changes(state)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 0
    finally:
        _reap(proc)


def test_a_stop_during_startup_drains_and_leaves_no_copy_behind(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    proc = _launch(tmp_path, startup_s=3.0, state=state)
    try:
        time.sleep(1.0)
        proc.send_signal(signal.SIGTERM)      # asked to stop while the first copy starts
        assert proc.wait(timeout=30) == 0, "a stop during startup ends cleanly"
        time.sleep(0.5)
        try:
            os.killpg(proc.pid, 0)            # the group must be empty: no copy was left behind
            leftover = True
        except ProcessLookupError:
            leftover = False
        assert not leftover, "the first copy outlived the supervisor"
    finally:
        _reap(proc)
