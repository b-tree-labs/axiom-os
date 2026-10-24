# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Real Postgres for the switch runner: containers named ``sw-<hex>``, never the
developer's database. Each is removed by name when the session ends."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.data_platform.tests import timescale_container


@pytest.fixture(scope="session")
def sw_dsn():
    import os

    os.environ.setdefault(timescale_container.PREFIX_ENV, "sw")
    name, url = timescale_container.start()
    yield url
    timescale_container.stop(name)


@pytest.fixture
def sw_db(sw_dsn):
    import psycopg

    with psycopg.connect(sw_dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS sw_t CASCADE")
        c.execute("CREATE SCHEMA sw_t")
        yield c
