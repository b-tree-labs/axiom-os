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


def _pg_available() -> bool:
    try:
        import psycopg2

        from axiom.infra.db import libpq_url, platform_db_url

        psycopg2.connect(libpq_url(platform_db_url()), connect_timeout=2).close()
        return True
    except Exception:  # noqa: BLE001 - any failure means no database to test against
        return False


@pytest.fixture
def attest_db(monkeypatch):
    """Provision a private attest schema at migration head; yield its name."""
    if not _pg_available():
        pytest.skip("Postgres not reachable; the attest store needs a real database")
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
