# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for Updater._check_migrations.

The real check_migrations() helper returns `pending` as an integer count and
the revision IDs in `pending_revisions`. A previous version of the caller
did len(pending) on the int and produced the cryptic error

    Could not check migrations: object of type 'int' has no len()

on every fresh laptop install (where the DB isn't reachable). Canary-quality
install feedback matters, so guard the shape of the contract with tests.
"""

from __future__ import annotations

from unittest.mock import patch

from axiom.extensions.builtins.update.cli import Updater


def test_not_connected_reports_skip():
    """When the DB isn't reachable, say so — don't claim a pending count."""
    updater = Updater()
    fake_status = {
        "current": None,
        "head": "001",
        "pending": 1,
        "pending_revisions": ["001"],
        "up_to_date": False,
        "connected": False,
    }
    with patch(
        "axiom.extensions.builtins.signals.migrations.check_migrations",
        return_value=fake_status,
    ):
        updater._check_migrations()

    assert len(updater.results) == 1
    result = updater.results[0]
    assert result.step == "migrations"
    assert result.success is True
    assert result.changed is False
    assert "not reachable" in result.message.lower()


def test_up_to_date():
    updater = Updater()
    fake_status = {
        "current": "001",
        "head": "001",
        "pending": 0,
        "pending_revisions": [],
        "up_to_date": True,
        "connected": True,
    }
    with patch(
        "axiom.extensions.builtins.signals.migrations.check_migrations",
        return_value=fake_status,
    ):
        updater._check_migrations()

    assert updater.results[0].message == "No pending migrations"
    assert updater.results[0].changed is False


def test_pending_count_uses_int_not_len():
    """This is the exact shape that broke v0.9.0 — pending is an int."""
    updater = Updater()
    fake_status = {
        "current": None,
        "head": "002",
        "pending": 2,  # <-- int, not list
        "pending_revisions": ["001", "002"],
        "up_to_date": False,
        "connected": True,
    }
    with patch(
        "axiom.extensions.builtins.signals.migrations.check_migrations",
        return_value=fake_status,
    ):
        updater._check_migrations()

    result = updater.results[0]
    assert result.changed is True
    assert "2 migration" in result.message
    assert "001" in result.details and "002" in result.details


def test_import_error_is_handled():
    updater = Updater()
    with patch(
        "axiom.extensions.builtins.signals.migrations.check_migrations",
        side_effect=ImportError("not installed"),
    ):
        updater._check_migrations()

    result = updater.results[0]
    assert result.success is True
    assert "not available" in result.message.lower()
