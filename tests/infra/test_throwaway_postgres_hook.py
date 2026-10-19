# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The pre-push hook gives the test run its own empty Postgres database and cleans up after it."""

from __future__ import annotations

import importlib.util
import os
import secrets
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "throwaway_postgres.py"


@pytest.fixture
def helper(real_postgres, monkeypatch):
    """The script as a module, pointed at the test server (the root fixture's throwaway database tells us which).

    Cleanup drops ONLY what this test made: databases named for this process (``create()`` names them by
    pid) and any explicitly registered in ``helper.made``. It must never sweep ``axiom_hook_*``: when this
    suite runs under the pre-push hook, the hook's own live database has that prefix, and dropping it
    mid-run broke every database test that came after.
    """
    spec = importlib.util.spec_from_file_location("throwaway_postgres_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    admin_url = real_postgres.replace("postgresql://", "postgresql+psycopg2://", 1)
    monkeypatch.setattr(mod, "_admin_urls", lambda: [admin_url])
    mod.made = []
    yield mod
    _cleanup(mod, admin_url)


def _cleanup(mod, admin_url) -> None:
    """Drop what this test process made, by name; never by the shared prefix."""
    admin = create_engine(
        make_url(admin_url).set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    with admin.connect() as c:
        mine = [
            r[0]
            for r in c.execute(
                text("select datname from pg_database where datname like :p"),
                {"p": f"{mod.PREFIX}{os.getpid()}\\_%"},
            )
        ]
        for name in sorted(set(mine) | set(mod.made)):
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def _databases(url) -> set[str]:
    admin = create_engine(make_url(url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        out = {r[0] for r in c.execute(text("select datname from pg_database"))}
    admin.dispose()
    return out


def test_create_makes_a_fresh_empty_database_and_drop_removes_it(helper, real_postgres, capsys):
    assert helper.create() == 0
    url = capsys.readouterr().out.strip()
    name = make_url(url).database
    assert name.startswith(helper.PREFIX) and name in _databases(real_postgres)
    eng = create_engine(url)
    with eng.connect() as c:
        tables = c.execute(
            text("select count(*) from information_schema.tables where table_schema = 'public'")
        ).scalar()
    eng.dispose()
    assert tables == 0, "the database the tests get must be EMPTY, like CI's"
    assert helper.drop(url) == 0
    assert name not in _databases(real_postgres)
    assert helper.drop(url) == 0, "dropping twice is fine"


def test_drop_refuses_a_database_it_did_not_create(helper, real_postgres, capsys):
    refused = helper.drop(real_postgres)
    assert refused == 3
    assert make_url(real_postgres).database in _databases(real_postgres), (
        "a refused drop must leave it alone"
    )
    assert "refusing to drop" in capsys.readouterr().err


def test_no_reachable_server_is_a_loud_failure_with_the_fix(helper, monkeypatch, capsys):
    monkeypatch.setattr(
        helper, "_admin_urls", lambda: ["postgresql+psycopg2://x:x@127.0.0.1:9/postgres"]
    )
    assert helper.create() == 2
    err = capsys.readouterr().err
    assert "AXIOM_PG_TEST_URL" in err and "nothing existing is touched" in err


def test_stale_orphans_are_reaped_but_a_busy_one_and_a_fresh_one_are_not(
    helper, real_postgres, capsys, monkeypatch
):
    admin = create_engine(
        make_url(real_postgres).set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    # A prefix of this run's own. Fixed names (axiom_hook_1_1000, ...) collided
    # with a second run against the same server: its CREATE failed "already
    # exists", and its teardown or its reaper (1000 reads as stale to everyone)
    # dropped this run's "busy" database before the holder connected. Under a
    # run prefix, this test's reaper sees only this run's databases, and every
    # other reaper parses these names as not its own and skips them. The real
    # 24 h threshold stays: a shorter one would let this test reap the
    # pre-push hook's own live database whenever it sat idle.
    monkeypatch.setattr(helper, "PREFIX", f"{helper.PREFIX}t{secrets.token_hex(4)}_")
    stale, busy, fresh = (
        f"{helper.PREFIX}1_1000_a",
        f"{helper.PREFIX}2_1000_b",
        f"{helper.PREFIX}3_{int(time.time())}_c",
    )
    helper.made.extend([stale, busy, fresh])  # dropped by name at teardown, never by prefix
    with admin.connect() as c:
        for n in (stale, busy, fresh):
            c.execute(text(f'CREATE DATABASE "{n}"'))
    holder = create_engine(
        make_url(real_postgres).set(database=busy)
    ).connect()  # something is connected
    try:
        assert helper.create() == 0
        url = capsys.readouterr().out.strip()
        left = _databases(real_postgres)
        assert stale not in left, "an old orphan nothing is connected to is reaped"
        assert busy in left, "a database someone is connected to is never reaped"
        assert fresh in left, "a recent one may belong to a run in progress"
    finally:
        holder.close()
        helper.drop(url)
        admin.dispose()


def test_cleanup_never_drops_a_database_it_did_not_make(helper, real_postgres):
    """Regression: the cleanup once dropped every axiom_hook_* database, including the pre-push hook's own
    live one, which killed every database test that ran after it."""
    admin_url = real_postgres.replace("postgresql://", "postgresql+psycopg2://", 1)
    tok = secrets.token_hex(4)  # two runs at once must not share these names either
    bystander = f"{helper.PREFIX}4242424_{int(time.time())}_{tok}"  # another process's live database
    mine = f"{helper.PREFIX}{os.getpid()}_{int(time.time())}_{tok}"
    admin = create_engine(
        make_url(admin_url).set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{bystander}"'))
            c.execute(text(f'CREATE DATABASE "{mine}"'))
        _cleanup(helper, admin_url)
        left = _databases(real_postgres)
        assert mine not in left
        assert bystander in left, "a database another process owns must survive this test's cleanup"
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{bystander}" WITH (FORCE)'))
        admin.dispose()


def test_two_creates_in_the_same_second_get_different_databases(helper, real_postgres, capsys):
    """The name used to be <pid>_<second>: a second create in the same second
    from the same process collided and reported 'no reachable server'."""
    assert helper.create() == 0
    first = make_url(capsys.readouterr().out.strip())
    assert helper.create() == 0
    second = make_url(capsys.readouterr().out.strip())
    try:
        assert first.database != second.database
        assert {first.database, second.database} <= _databases(real_postgres)
    finally:
        helper.drop(first.render_as_string(hide_password=False))
        helper.drop(second.render_as_string(hide_password=False))


@pytest.mark.parametrize(
    "name,expected",
    [("axiom_hook_123_1700000000", 1700000000), ("axiom_hook_123_1700000000_ab12cd34", 1700000000),
     ("axiom_hook_x", None), ("axiom_test_123_1700000000", None), ("axiom_hook_123_abc", None)],
)
def test_the_reaper_reads_the_time_from_old_and_new_names(helper, name, expected):
    """Orphans named the old way are still reaped after the format change."""
    assert helper.made_at(name) == expected


def test_a_run_prefix_is_invisible_to_every_other_reaper(helper):
    """What makes the test's per-run prefix safe: a reaper using the real prefix
    reads such a name as not one of its own, so it never drops it."""
    assert helper.made_at(f"{helper.PREFIX}tdeadbeef_1_1000_a") is None
