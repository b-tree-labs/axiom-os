# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The kit deletes and rewrites rows only in a database ``kit-up`` stamped.

``kit-try`` begins by deleting every silver row for the tenant, then conforms
the samples back in, and ``kit-up`` turns on row-level security. That is right
on a disposable medallion and destructive anywhere else, and ``AXIOM_KIT_DSN``
accepts any Postgres. So a database holding real rows must be refused, and the
proof is that the rows are still there afterwards, untouched, and that no
policy or role was left behind.

Each test makes its own scratch database on the kit's server, so the shared
medallion the other kit tests use is never the subject.
"""

from __future__ import annotations

import os
import shutil
import uuid

import pytest

from axiom.extensions.builtins.data_platform import kit
from axiom.extensions.builtins.data_platform.kit import runner
from axiom.extensions.builtins.data_platform.kit.medallion import connect, dsn, is_stamped
from axiom.extensions.builtins.data_platform.kit.project import KitError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (shutil.which("docker") or os.environ.get("AXIOM_KIT_DSN")),
        reason="needs Docker or AXIOM_KIT_DSN",
    ),
]

TENANT = "guard-site"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """A stamped medallion whose SERVER the tests borrow to make scratch databases."""
    repo = tmp_path_factory.mktemp("guard-host")
    kit.init(repo, TENANT)
    host = kit.load(repo)
    kit.up(host)
    yield dsn(host)
    kit.down(host)


@pytest.fixture
def scratch(server):
    """An empty, unstamped database on the same server; dropped afterwards."""
    name = f"kit_guard_{uuid.uuid4().hex[:10]}"
    with connect(server) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    parts = conninfo_to_dict(server)
    parts["dbname"] = name
    yield make_conninfo(**parts)
    with connect(server) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _real_database(d: str) -> None:
    """Give a scratch database what a real one has: silver, holding this tenant's rows."""
    from axiom.extensions.builtins.data_platform.conformance import (
        SILVER_DDL,
        apply_conformance_ddl,
    )

    with connect(d) as conn, conn.cursor() as cur:
        apply_conformance_ddl(cur, SILVER_DDL)
        for i in range(3):
            cur.execute(
                "INSERT INTO silver.signals (site, feed, channel, ts, value, unit, schema_ref,"
                " row_hash) VALUES (%s, 'live', 'history', %s, %s, 'degC', 'real', %s)",
                (TENANT, f"2025-01-0{i + 1}T00:00:00+00:00", float(i), f"real-{i}"),
            )


def _untouched(d: str) -> None:
    """The three real rows are exactly as written, and nothing kit-shaped was added."""
    with connect(d) as conn:
        rows = conn.execute(
            "SELECT row_hash, value FROM silver.signals ORDER BY row_hash"
        ).fetchall()
        assert rows == [("real-0", 0.0), ("real-1", 1.0), ("real-2", 2.0)]
        rls = conn.execute(
            "SELECT relrowsecurity FROM pg_class WHERE oid = 'silver.signals'::regclass"
        ).fetchone()[0]
        assert rls is False, "row-level security was turned on in a database the kit refused"
    assert not is_stamped(d)


@pytest.fixture
def project(tmp_path, monkeypatch, scratch):
    kit.init(tmp_path, TENANT)
    monkeypatch.setenv("AXIOM_KIT_DSN", scratch)
    return kit.load(tmp_path)


def test_kit_try_refuses_an_unstamped_database_and_touches_nothing(project, scratch):
    _real_database(scratch)
    with pytest.raises(KitError, match=r"not a kit medallion.*destroys the site's history"):
        kit.try_kit(project)
    _untouched(scratch)


def test_every_writing_step_refuses_on_its_own(project, scratch):
    """Defence in depth: a caller that skips try_kit still cannot write."""
    from axiom.extensions.builtins.data_platform.kit import declarations as decl
    from axiom.extensions.builtins.data_platform.kit.check import isolation_findings

    _real_database(scratch)
    ds = decl.load(project)
    for step in (
        lambda: runner.conform(project, scratch),
        lambda: runner.apply_silver(project, scratch, ds),
        lambda: runner.apply_gold(project, scratch, ds),
        lambda: isolation_findings(project, scratch, ds),
        lambda: kit.medallion.prepare(project, scratch),
    ):
        with pytest.raises(KitError, match="not a kit medallion"):
            step()
    _untouched(scratch)


def test_kit_up_refuses_to_stamp_a_database_that_holds_rows(project, scratch):
    _real_database(scratch)
    with pytest.raises(KitError, match=r"already holds rows.*destroy that site's history"):
        kit.up(project)
    _untouched(scratch)


def test_the_refusal_never_prints_a_password(project, scratch):
    from axiom.extensions.builtins.data_platform.kit.medallion import where

    assert "hunter2" not in where("postgresql://u:hunter2@db.example:6543/prod")
    assert where("postgresql://u:hunter2@db.example:6543/prod") == "db.example:6543/prod"


def test_an_empty_database_is_stamped_by_kit_up_and_then_runs(project, scratch):
    """The positive control: the guard does not stop the kit doing its job."""
    kit.up(project)
    assert is_stamped(scratch)
    report = kit.try_kit(project)
    assert report["silver"]["rows_out"] == 3 * 1008
