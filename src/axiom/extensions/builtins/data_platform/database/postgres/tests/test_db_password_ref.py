# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the SEC-3 ``--db-password-ref`` wiring."""

from __future__ import annotations

import argparse

import pytest

from axiom.extensions.builtins.data_platform.database.postgres.provider import (
    PostgresDatabaseProvider,
)


def _ns(**kwargs) -> argparse.Namespace:
    defaults = dict(
        db_mode="internal",
        db_dsn="",
        db_password="",
        db_password_ref="",
        db_database="axiom",
        db_username="axiom",
        db_storage="20Gi",
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_install_args_register_both_flags():
    parser = argparse.ArgumentParser()
    PostgresDatabaseProvider().add_install_args(parser)
    help_text = parser.format_help()
    assert "--db-password" in help_text
    assert "--db-password-ref" in help_text


def test_literal_password_wins_when_alone():
    out = PostgresDatabaseProvider().helm_values(_ns(db_password="hunter2"))
    assert out["database.internal.password"] == "hunter2"


def test_env_ref_resolves_at_install_time(monkeypatch):
    monkeypatch.setenv("MY_PG_PASSWORD", "via-env")
    out = PostgresDatabaseProvider().helm_values(
        _ns(db_password_ref="env://MY_PG_PASSWORD"),
    )
    assert out["database.internal.password"] == "via-env"


def test_dsn_uses_resolved_password(monkeypatch):
    monkeypatch.setenv("MY_PG_PASSWORD", "via-env")
    dsn = PostgresDatabaseProvider().construct_dsn(
        _ns(db_password_ref="env://MY_PG_PASSWORD"),
    )
    assert "via-env" in dsn
    assert dsn.startswith("postgresql://axiom:via-env@")


def test_mutually_exclusive_password_and_ref_rejected():
    with pytest.raises(ValueError, match="mutually exclusive"):
        PostgresDatabaseProvider().helm_values(
            _ns(db_password="literal", db_password_ref="env://X"),
        )


def test_external_mode_ignores_password_ref():
    out = PostgresDatabaseProvider().helm_values(
        _ns(
            db_mode="external",
            db_dsn="postgresql://prod:secret@db:5432/app",
            db_password_ref="env://UNUSED",
        ),
    )
    assert out["database.mode"] == "external"
    assert "database.internal.password" not in out
    assert out["database.external.dsn"].startswith("postgresql://prod:")


def test_no_password_at_all_omits_helm_value():
    out = PostgresDatabaseProvider().helm_values(_ns())
    # Chart requires it at template time; resolver doesn't synthesize.
    assert "database.internal.password" not in out


def test_dsn_uses_placeholder_when_no_password():
    dsn = PostgresDatabaseProvider().construct_dsn(_ns())
    assert "<password>" in dsn


def test_unresolvable_ref_propagates_clean_error():
    # No env var, openbao not running.
    with pytest.raises((KeyError, RuntimeError)):
        PostgresDatabaseProvider().helm_values(
            _ns(db_password_ref="env://__NO_SUCH_VAR_XYZ"),
        )
