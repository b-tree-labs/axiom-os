# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`update` must not report a migration it did not observe land.

Three defects, all the same shape.

`run_migrations` returns a bool and catches its own exceptions — it prints the
alembic error and returns False. `_run_migrations` discarded that return and
appended "Migrations applied" regardless, which means the `except` clause below
it could never fire for an actual migration failure: the exception had already
been swallowed one frame down. The operator saw a stray "Migration error:" line
on stdout and a green "Migrations applied" underneath it.

Nothing then confirmed the revision moved. `check_migrations()` answers exactly
that question and was already being called *before* the upgrade; asking again
afterwards is the whole fix.

And a database that could not be reached was reported as success with the
reason discarded — connection refused, expired password, wrong host and bad
cert all collapsing into one cheerful "Database not available, skipping". An
operator whose credentials just expired sees green and learns about it later,
somewhere else.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.update.cli import Updater


@pytest.fixture
def updater(tmp_path):
    return Updater(repo_root=tmp_path)


def _drive(updater, monkeypatch, *, statuses, upgrade_ok=True, connect_error=None):
    """Run _run_migrations against scripted alembic helpers."""
    import axiom.extensions.builtins.signals.migrations as mig

    seen = iter(statuses)
    monkeypatch.setattr(mig, "check_migrations", lambda: next(seen, statuses[-1]))
    monkeypatch.setattr(
        mig, "run_migrations", lambda *a, **k: upgrade_ok, raising=False
    )

    class _Conn:
        def close(self): ...

    import psycopg2  # noqa: F401  (import guarded below)

    def fake_connect(*a, **k):
        if connect_error is not None:
            raise connect_error
        return _Conn()

    monkeypatch.setattr("psycopg2.connect", fake_connect)
    updater._run_migrations()
    return updater.results[-1]


_BEHIND = {"up_to_date": False, "current": "a1", "head": "b2", "pending": 1}
_ARRIVED = {"up_to_date": True, "current": "b2", "head": "b2", "pending": 0}


def test_a_failed_upgrade_is_not_reported_as_applied(updater, monkeypatch):
    """run_migrations returning False must not become 'Migrations applied'."""
    result = _drive(
        updater, monkeypatch, statuses=[_BEHIND, _BEHIND], upgrade_ok=False
    )
    assert not result.success, "a failed alembic upgrade was reported as applied"


def test_an_upgrade_that_did_not_move_the_revision_is_a_failure(updater, monkeypatch):
    """alembic can report success while leaving the revision where it was."""
    result = _drive(
        updater, monkeypatch, statuses=[_BEHIND, _BEHIND], upgrade_ok=True
    )
    assert not result.success, "the revision never reached head but we said applied"
    assert "a1" in (result.message + result.details)


def test_a_migration_that_landed_is_reported_with_the_revision(updater, monkeypatch):
    result = _drive(
        updater, monkeypatch, statuses=[_BEHIND, _ARRIVED], upgrade_ok=True
    )
    assert result.success and result.changed
    assert "b2" in result.message


def test_an_unreachable_database_says_why(updater, monkeypatch):
    """Skipping is fine. Discarding the reason is not."""
    result = _drive(
        updater,
        monkeypatch,
        statuses=[_BEHIND, _BEHIND],
        connect_error=RuntimeError("password authentication failed for user 'axiom'"),
    )
    assert not result.changed
    blob = result.message + result.details
    assert "password authentication failed" in blob, (
        "the connection failure reason was discarded"
    )
