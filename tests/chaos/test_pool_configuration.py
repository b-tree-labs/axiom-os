# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Scale: the connection pool is a DEPLOYMENT parameter, so it is configurable.

Measured against a local PostgreSQL rather than guessed:

- the stock pool is size 5 with overflow 10, so one process holds at most 15
  connections. At concurrency 32 the median wait doubled (64 ms → 111 ms) with
  no errors — that is the pool queueing, not the database struggling;
- PostgreSQL's `max_connections` was 100, so six such processes (6 x 15 = 90)
  exhaust it. Pool size therefore decides how many workers a node can run;
- `pool_recycle` was -1: connections were NEVER recycled, so a long-lived
  worker behind a proxy that drops idle connections keeps handing out dead
  ones.
"""

from __future__ import annotations

import pytest

from axiom.infra.db import _positive_int, pool_settings


class TestMeasuredDefaults:
    def test_connections_are_recycled(self):
        """-1 means never. The proxy's idle timeout is usually the shorter
        deadline, and pre-ping costs a round trip to discover the same thing."""
        assert pool_settings()["pool_recycle"] == 1800

    def test_the_documented_defaults_hold(self):
        settings = pool_settings()
        assert settings["pool_size"] == 5
        assert settings["max_overflow"] == 10
        assert settings["pool_timeout"] == 30

    def test_every_setting_is_overridable(self, monkeypatch):
        """Six processes at the default exhaust a stock PostgreSQL, so an
        operator has to be able to change this without editing code."""
        for name, key in (
            ("AXIOM_DB_POOL_SIZE", "pool_size"),
            ("AXIOM_DB_MAX_OVERFLOW", "max_overflow"),
            ("AXIOM_DB_POOL_TIMEOUT", "pool_timeout"),
            ("AXIOM_DB_POOL_RECYCLE", "pool_recycle"),
        ):
            monkeypatch.setenv(name, "7")
            assert pool_settings()[key] == 7
            monkeypatch.delenv(name)


class TestSqliteTakesNoPoolSettings:
    """SQLite uses SingletonThreadPool, which accepts neither `max_overflow`
    nor `pool_timeout`. Passing them raises at engine construction, which
    would break every local-testing path — as the existing pre-ping test
    caught when this first went in."""

    def test_sqlite_gets_no_pool_arguments(self):
        assert pool_settings("sqlite:///:memory:") == {}
        assert pool_settings("sqlite://") == {}

    def test_a_server_database_still_gets_them(self):
        """Pinned so the SQLite guard cannot quietly disable pooling
        everywhere and still look correct."""
        settings = pool_settings("postgresql://user@host/db")
        assert settings["pool_size"] == 5
        assert settings["pool_recycle"] == 1800

    def test_an_sqlite_engine_actually_builds(self):
        from sqlalchemy import create_engine

        url = "sqlite:///:memory:"
        engine = create_engine(
            url, future=True, pool_pre_ping=True, **pool_settings(url)
        )
        assert engine is not None


class TestMisconfigurationIsSurvivable:
    """A node that refuses to start because someone typo'd a number is worse
    than one running on the documented default."""

    @pytest.mark.parametrize("bad", ["", "  ", "abc", "5.5", "-1", "1e3"])
    def test_an_unusable_value_falls_back(self, monkeypatch, bad):
        monkeypatch.setenv("AXIOM_DB_POOL_SIZE", bad)
        assert _positive_int("AXIOM_DB_POOL_SIZE", 5) == 5

    def test_a_usable_value_is_honoured(self, monkeypatch):
        """Pinned so the fallback cannot quietly swallow every value and
        still look correct."""
        monkeypatch.setenv("AXIOM_DB_POOL_SIZE", "20")
        assert _positive_int("AXIOM_DB_POOL_SIZE", 5) == 20

    def test_zero_is_honoured_not_treated_as_absent(self, monkeypatch):
        """0 is meaningful to SQLAlchemy — with max_overflow it means
        unbounded — so it must not be confused with 'unset'."""
        monkeypatch.setenv("AXIOM_DB_MAX_OVERFLOW", "0")
        assert _positive_int("AXIOM_DB_MAX_OVERFLOW", 10) == 0
