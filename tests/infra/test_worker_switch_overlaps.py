# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A worker with no socket is switched by overlap (ADR-182 D3).

The new copy is running before the old one is told to drain, so work never
stops; the old copy finishes the job it holds; and the new copy is told it is
the only one (SIGUSR1) only after the old copy has exited, because that is the
moment pending work can safely be recovered. Real processes, a real journal.
"""

from __future__ import annotations

import os
import sys
import textwrap
import time
from pathlib import Path

from axiom.infra.switch import Supervisor

WORKER = textwrap.dedent(
    """
    import os, signal, sys, time
    journal = os.environ["JOURNAL"]
    me = os.environ["COPY"]

    def log(event):
        with open(journal, "a") as fh:
            fh.write(f"{time.monotonic():.6f} {me} {event}\\n")

    stop = False
    def on_term(*_):
        global stop
        stop = True
        log("drain")
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGUSR1, lambda *_: log("sole"))
    if os.environ.get("NEVER_READY"):
        time.sleep(60)
    log("start")
    fd = int(os.environ.pop("AXI_READY_FD"))
    os.write(fd, b"R"); os.close(fd)
    while not stop:
        log("job-begin")
        time.sleep(0.4)   # a job: once begun it is finished, never abandoned
        log("job-end")
    log("exit")
    """
)


def _journal(path: Path) -> list[tuple[float, str, str]]:
    rows = []
    for line in path.read_text().splitlines():
        t, who, event = line.split()
        rows.append((float(t), who, event))
    return rows


def _first(rows, who, event):
    return next(t for t, w, e in rows if w == who and e == event)


def test_a_worker_switch_overlaps_and_hands_over_cleanly(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text(WORKER)
    journal = tmp_path / "journal"
    env = {**os.environ, "JOURNAL": str(journal)}
    sup = Supervisor(
        [sys.executable, str(script)],
        env={**env, "COPY": "old"},
        listen=False,
        drain_s=5.0,
        ready_timeout_s=20.0,
    )
    sup.start()
    try:
        time.sleep(1.0)
        result = sup.switch(env={**env, "COPY": "new"}, subject="v2")
        assert result.outcome == "switched", result.detail
        time.sleep(1.0)
    finally:
        sup.stop()

    rows = _journal(journal)
    # Overlap: the new copy started before the old one was told to drain.
    assert _first(rows, "new", "start") < _first(rows, "old", "drain")
    # The old copy finished every job it began.
    old = [e for _, w, e in rows if w == "old"]
    assert old.count("job-begin") == old.count("job-end"), old
    # "sole" reached the new copy only after the old copy had exited.
    assert _first(rows, "old", "exit") < _first(rows, "new", "sole")
    # The first copy was alone when it started, and was told so then; it is
    # never told so again once a second copy exists.
    new_started = _first(rows, "new", "start")
    assert [e for t, w, e in rows if w == "old" and e == "sole" and t > new_started] == []
    assert old.count("sole") == 1


def test_a_worker_that_never_becomes_ready_leaves_the_old_one_alone(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text(WORKER)
    journal = tmp_path / "journal"
    env = {**os.environ, "JOURNAL": str(journal)}
    sup = Supervisor(
        [sys.executable, str(script)], env={**env, "COPY": "old"}, listen=False, drain_s=5.0
    )
    sup.start()
    try:
        result = sup.switch(
            env={**env, "COPY": "bad", "NEVER_READY": "1"}, subject="vbad", ready_timeout_s=2.0
        )
        assert result.outcome == "rejected_before_switch"
        time.sleep(0.5)
    finally:
        sup.stop()
    rows = _journal(journal)
    old = [e for _, w, e in rows if w == "old"]
    # The old copy kept working through the failed attempt, and was only
    # drained by the final stop.
    assert old.count("drain") == 1 and old[-1] == "exit"
    assert not any(w == "bad" for _, w, _ in rows)
