# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
""""Not installed" is a third outcome, and must not read as "broken".

A deploy ran migrations on a node for the first time and failed on `signals`:

    Migration error: No module named 'pgvector'
       signals:  ❌ upgrade failed

Nothing was broken. `pgvector` lives in the `signal` extra and the node
installs `axiom-os-lm` without extras, so there is no signals code there to
migrate. Counting that as failure means a deploy can never complete on any node
that deliberately does not ship every extension — and a step that can never go
green is a step people route around.
"""

from __future__ import annotations

from axiom.infra.db import ProvisionResult


def test_a_skip_is_not_a_failure():
    r = ProvisionResult(
        extension="signals", schema="signals", before="", after="", ok=True,
        skipped="'pgvector' is not installed — this extension's dependencies "
                "are not part of this install (check its optional extra)",
    )
    assert r.ok, "a skip must not fail the caller"
    assert not r.migrated, "nothing moved, so it did not migrate"
    assert "skipped" in r.summary
    assert "pgvector" in r.summary


def test_a_skip_is_not_a_success_either():
    """`migrated` stays False so 'skipped' is never mistaken for 'up to date'."""
    skipped = ProvisionResult(
        extension="signals", schema="signals", before="0001", after="0001",
        ok=True, skipped="'pgvector' is not installed",
    )
    current = ProvisionResult(
        extension="authz", schema="authz", before="0001", after="0001", ok=True,
    )
    assert not skipped.migrated and not current.migrated
    assert "skipped" in skipped.summary
    assert "up to date" in current.summary
    assert skipped.summary != current.summary


def test_broken_still_reads_as_broken():
    """The distinction is the point: one field for both would erase it."""
    r = ProvisionResult(
        extension="schedule", schema="schedule", before="", after="", ok=False,
        error='ProgrammingError: relation "schedule_definition" does not exist',
    )
    assert not r.ok
    assert "failed" in r.summary
    assert "skipped" not in r.summary


def test_skipped_and_error_are_separate_fields():
    """So an operator can react differently, and a log can be filtered."""
    assert ProvisionResult("x", "x", "", "", True, skipped="gone").error == ""
    assert ProvisionResult("x", "x", "", "", False, error="bang").skipped == ""


def test_a_missing_module_skips_rather_than_fails(monkeypatch, tmp_path):
    """The real path: provision_extension catches ModuleNotFoundError."""
    import axiom.infra.db as db

    monkeypatch.setattr(db, "ensure_schema", lambda *a, **k: "ghost")
    monkeypatch.setattr(db, "get_engine", lambda *a, **k: object())

    def _boom(*a, **k):
        raise ModuleNotFoundError("No module named 'pgvector'", name="pgvector")

    monkeypatch.setattr(db, "current_revision", _boom)

    result = db.provision_extension("ghost", migrations_dir=tmp_path)
    assert result.ok, "a missing dependency must not fail the deploy"
    assert "pgvector" in result.skipped
    assert result.error == ""


def test_a_real_migration_error_still_fails(monkeypatch, tmp_path):
    """Negative control on the same path — only ModuleNotFoundError skips."""
    import axiom.infra.db as db

    monkeypatch.setattr(db, "ensure_schema", lambda *a, **k: "ghost")
    monkeypatch.setattr(db, "get_engine", lambda *a, **k: object())

    def _boom(*a, **k):
        raise RuntimeError('relation "x" does not exist')

    monkeypatch.setattr(db, "current_revision", _boom)

    result = db.provision_extension("ghost", migrations_dir=tmp_path)
    assert not result.ok
    assert result.skipped == ""
    assert "does not exist" in result.error
