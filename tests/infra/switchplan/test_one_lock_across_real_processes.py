# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-197 D7: one single-flight lock, in real processes and on a real database.

A killed holder releases: the lease by its dead pid, the advisory lock with its
connection. Nothing here is mocked.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time

import pytest

from axiom.infra.switchplan.errors import SwitchBusy
from axiom.infra.switchplan.lock import SingleFlight

HOLDER = textwrap.dedent(
    """
    import sys, time
    from axiom.infra.switchplan.lock import SingleFlight
    lock = SingleFlight(sys.argv[1], lease_dir=sys.argv[2])
    assert lock.acquire()
    print("held", flush=True)
    time.sleep(120)
    """
)


def _spawn_holder(name, lease_dir):
    p = subprocess.Popen([sys.executable, "-c", HOLDER, name, str(lease_dir)], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def test_a_lease_is_refused_while_a_live_process_holds_it_and_taken_after_it_is_killed(tmp_path):
    holder = _spawn_holder("switch:x", tmp_path)
    try:
        mine = SingleFlight("switch:x", lease_dir=tmp_path)
        assert mine.acquire() is False
        with pytest.raises(SwitchBusy):
            with SingleFlight("switch:x", lease_dir=tmp_path):
                pass
        holder.kill()
        holder.wait(timeout=10)
        deadline = time.monotonic() + 10
        while not mine.acquire():
            assert time.monotonic() < deadline, "a killed holder's lease was never taken over"
            time.sleep(0.1)
        mine.release()
    finally:
        holder.kill()


def test_different_names_do_not_contend(tmp_path):
    a, b = SingleFlight("a", lease_dir=tmp_path), SingleFlight("b", lease_dir=tmp_path)
    assert a.acquire() and b.acquire()
    a.release()
    b.release()


def test_an_advisory_lock_is_refused_to_a_second_connection_and_released_when_the_holder_dies(sw_dsn):
    import psycopg

    holder = psycopg.connect(sw_dsn, autocommit=True)
    other = psycopg.connect(sw_dsn, autocommit=True)
    admin = psycopg.connect(sw_dsn, autocommit=True)
    try:
        a, b = SingleFlight("swap:t", conn=holder), SingleFlight("swap:t", conn=other)
        assert a.acquire() is True
        assert b.acquire() is False
        pid = holder.info.backend_pid
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        deadline = time.monotonic() + 10
        while not b.acquire():
            assert time.monotonic() < deadline, "the dead holder's advisory lock was never released"
            time.sleep(0.1)
        b.release()
    finally:
        for c in (holder, other, admin):
            try:
                c.close()
            except Exception:
                pass


def test_blocking_mode_waits_up_to_the_timeout(tmp_path):
    held = SingleFlight("w", lease_dir=tmp_path)
    assert held.acquire()
    t0 = time.monotonic()
    assert SingleFlight("w", lease_dir=tmp_path).blocking(0.3) is False
    assert 0.25 <= time.monotonic() - t0 < 3
    held.release()
