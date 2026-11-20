# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A real TimescaleDB for the tests that need one.

Started once per session in a container (``tsdb-<hex>``, see
:mod:`..tests.timescale_container`). Without docker the tests skip locally
and fail on CI, where a skipped test would hide a broken guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from axiom.extensions.builtins.data_platform.conformance import SILVER_SIGNALS_DDL
from axiom.extensions.builtins.data_platform.tests import timescale_container


@pytest.fixture(scope="session")
def timescale_dsn():
    name, url = timescale_container.start()
    yield url
    timescale_container.stop(name)


@dataclass(frozen=True)
class SmallDisk:
    """A throwaway server and the data volume's container name.

    The name is how a test puts an ordinary file on the data tmpfs. Relation
    files are the wrong filler: PostgreSQL keeps them open until a checkpoint,
    and a checkpoint that cannot create ``pg_logical/replorigin_checkpoint.tmp``
    PANICs the whole server.
    """

    name: str
    dsn: str


@pytest.fixture
def small_disk_timescale_dsn():
    """A TimescaleDB whose table files live on a small tmpfs and whose WAL has
    its own, larger one, so filling the data disk fails statements rather
    than the server. (On one shared volume a full disk fails WAL first, and
    PostgreSQL PANICs and exits: measured 2026-10-08.)

    Checkpoints do not start on their own. ``max_wal_size=96MB`` used to, in
    the middle of the fill, and a checkpoint with no room for
    ``pg_logical/replorigin_checkpoint.tmp`` PANICs (measured: the client then
    sees "server closed the connection unexpectedly")."""
    name, url = timescale_container.start(
        [
            "--tmpfs", "/pgdata:rw,size=128m",
            "--tmpfs", "/pgwal:rw,size=256m",
            "-e", "PGDATA=/pgdata/data",
            "-e", "POSTGRES_INITDB_WALDIR=/pgwal/wal",
        ],
        [
            "postgres",
            "-c", "max_wal_size=200MB",
            "-c", "min_wal_size=32MB",
            "-c", "checkpoint_timeout=1d",
        ],
    )
    yield SmallDisk(name, url)
    timescale_container.stop(name)


@pytest.fixture
def conn(timescale_dsn):
    import psycopg

    with psycopg.connect(timescale_dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS silver CASCADE")
        c.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        for stmt in SILVER_SIGNALS_DDL:
            c.execute(stmt)
        yield c
