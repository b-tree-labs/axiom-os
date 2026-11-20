# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A real TimescaleDB (``tsdb-<hex>`` container) for the contract, adoption and
storage tests. Never the developer's database."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.data_platform.tests import timescale_container

SCHEMA = "ts_test"


@pytest.fixture(scope="session")
def timescale_dsn():
    name, url = timescale_container.start()
    yield url
    timescale_container.stop(name)


@pytest.fixture
def db(timescale_dsn):
    """An autocommit connection with a fresh ``ts_test`` schema."""
    import psycopg

    with psycopg.connect(timescale_dsn, autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        c.execute(f"CREATE SCHEMA {SCHEMA}")
        yield c
