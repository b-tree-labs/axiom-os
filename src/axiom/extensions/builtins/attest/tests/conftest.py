# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Real Postgres for the attest store. The append-only guard is a Postgres
trigger, so a SQLite stand-in would test nothing that matters.

Each test gets its own schema, provisioned through the same Alembic path a
node uses (``provision_extension``) and dropped afterwards, so a developer's
live ``attest`` schema is never touched.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from axiom.attest.chain import Ed25519Signer
from axiom.vega.identity.keypair import generate_keypair

DEMO_LOGBOOK = Path(__file__).parent / "logbooks" / "demo_log.toml"


@pytest.fixture
def attest_db(monkeypatch, real_postgres):
    """Provision a private attest schema at migration head; yield its name.

    Rides the root ``real_postgres`` fixture, not its own reachability probe. The
    suite's guard points ``AXIOM_DB_URL`` at a refusing address when nothing chose a
    database, so a probe of the platform URL always failed and every test here
    skipped on CI too, service container or not. ``real_postgres`` finds the server
    through ``AXIOM_PG_TEST_URL`` / the default DSN and, on CI, fails rather than skips.
    """
    from sqlalchemy import text

    from axiom.extensions.builtins.attest import registry, store
    from axiom.infra.db import engine_for, normalize_extension_name, provision_extension

    monkeypatch.setenv("AXIOM_TEST_SCHEMA_SUFFIX", f"_t{uuid.uuid4().hex[:10]}")
    schema = normalize_extension_name("attest")
    result = provision_extension("attest")
    assert result.ok, result.error
    store.reset_provider()
    registry.reset()
    registry.register_file(DEMO_LOGBOOK)
    try:
        yield schema
    finally:
        registry.reset()
        engine, _ = engine_for("attest")
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.fixture
def node_signer():
    return Ed25519Signer(key_id="node:test", keypair=generate_keypair())
