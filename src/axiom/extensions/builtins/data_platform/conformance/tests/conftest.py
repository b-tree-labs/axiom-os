# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A real TimescaleDB for the tests that need one.

Started once per session in a container. Without docker the tests skip
locally and fail on CI, where a skipped test would hide a broken guarantee.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid

import pytest

from axiom.extensions.builtins.data_platform.conformance import SILVER_SIGNALS_DDL

IMAGE = "timescale/timescaledb:2.17.2-pg16"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(extra: list[str], postgres_args: list[str] = ()):
    if shutil.which("docker") is None or subprocess.run(
        ["docker", "info"], capture_output=True
    ).returncode != 0:
        if os.environ.get("CI"):
            pytest.fail("docker is required on CI for the TimescaleDB tests")
        pytest.skip("docker is not available")
    # A prefix lets a shared machine name (and remove by name) its own
    # containers; the default is unchanged.
    prefix = os.environ.get("AXIOM_TEST_CONTAINER_PREFIX") or "axiom-ts-test"
    name = f"{prefix}-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=ts-test-only",
         "-p", f"127.0.0.1:{port}:5432", *extra, IMAGE, *postgres_args],
        check=True, capture_output=True,
    )
    url = f"postgresql://postgres:ts-test-only@127.0.0.1:{port}/postgres"
    import psycopg

    deadline = time.monotonic() + 120
    while True:
        try:
            with psycopg.connect(url, connect_timeout=2) as c:
                c.execute("SELECT 1")
            break
        except Exception:
            if time.monotonic() > deadline:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True)
                raise
            time.sleep(1)
    return name, url


@pytest.fixture(scope="session")
def timescale_dsn():
    name, url = _start([])
    yield url
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
def small_disk_timescale_dsn():
    """A TimescaleDB whose table files live on a small tmpfs and whose WAL has
    its own, larger one, so filling the data disk fails statements rather
    than the server. (On one shared volume a full disk fails WAL first, and
    PostgreSQL PANICs and exits: measured 2026-10-08.)"""
    name, url = _start(
        [
            "--tmpfs", "/pgdata:rw,size=128m",
            "--tmpfs", "/pgwal:rw,size=256m",
            "-e", "PGDATA=/pgdata/data",
            "-e", "POSTGRES_INITDB_WALDIR=/pgwal/wal",
        ],
        ["postgres", "-c", "max_wal_size=96MB", "-c", "min_wal_size=32MB"],
    )
    yield url
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
def conn(timescale_dsn):
    import psycopg

    with psycopg.connect(timescale_dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS silver CASCADE")
        c.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        for stmt in SILVER_SIGNALS_DDL:
            c.execute(stmt)
        yield c


