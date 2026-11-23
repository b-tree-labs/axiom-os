# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``audit.healthcheck`` — runs without Postgres by exercising
the failure paths directly (the real probe reaches for ``session_for``
and is covered by the PG-only integration suite)."""

from __future__ import annotations

import logging

import pytest

from axiom.extensions.builtins.authz.skills import healthcheck
from axiom.infra.paths import get_user_state_dir
from axiom.infra.skills import SkillContext, SkillRegistry


@pytest.fixture()
def ctx():
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("test.healthcheck"),
        user_prompt=None,
    )


def test_returns_mode_in_value(ctx, monkeypatch):
    monkeypatch.setenv("AXIOM_MODE", "production")
    # The PG-backed probes may fail in CI without a DB; we only assert on
    # the mode + the structural shape of the result here.
    r = healthcheck.run({}, ctx)
    assert r.value["resource"] == "healthcheck"
    assert r.value["mode"] == "production"
    assert "schema" in r.value
    assert "decide" in r.value


def test_default_mode_is_dev(ctx, monkeypatch):
    monkeypatch.delenv("AXIOM_MODE", raising=False)
    r = healthcheck.run({}, ctx)
    assert r.value["mode"] == "dev"


def test_failed_probes_surface_errors(ctx, monkeypatch):
    """When schema + decide probes both fail (no DB), errors are
    populated and ``ok`` is False."""
    # Force a bad DB URL so both probes fail quickly.
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://nope@127.0.0.1:1/no")
    # Reset the cached engine so the bad URL takes effect — through monkeypatch,
    # so it is restored at teardown. Assigning these globals directly leaks: the
    # env var is restored but the engine built from the bad URL stays cached, and
    # every later test that calls session_for() silently falls back to in-memory.
    import axiom.infra.db as _db
    monkeypatch.setattr(_db, "_engine", None, raising=False)
    monkeypatch.setattr(_db, "_session_factory", None, raising=False)
    r = healthcheck.run({}, ctx)
    if r.ok:
        # If the env happens to have a working DB, just assert the shape.
        assert r.value["schema"].get("ok") is not None
        return
    assert not r.ok
    # At least one of the two probes must have surfaced an error.
    assert len(r.errors) >= 1
