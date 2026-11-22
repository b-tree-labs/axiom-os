# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Kit promotion against a node that behaves like a live one, on a real Postgres.

Three things a live run hit that a superuser sandbox never shows:

- the serving app logs in with a read-only role. A submit ran ``CREATE SCHEMA``
  every time ("permission denied for database") and the door answered 500. A
  submit now runs no DDL; ``kit-host-setup`` makes the registry once, as the
  writer, and grants the serving login exactly what it needs.
- ``silver.signals`` is never idle. An approve took ACCESS EXCLUSIVE on it every
  time (policy drop and create, security on), waited 5 s behind the readers, and
  then marked a good bundle ``rejected`` for ever. Now the policies are made once,
  a lock not granted is retried, and a bundle that still cannot get one stays
  ``checked`` with the error recorded.
- kit-try deleted every silver row of the site before conforming, so a snapshot
  loaded with kit-materialize never survived into it, and a starter scaffolded
  over that snapshot had nothing to draw.

Needs Docker (or AXIOM_KIT_DSN); marked ``integration``. Every database here is
one kit-up started; nothing reads or writes a developer's database.
"""

from __future__ import annotations

import os
import shutil

import pytest

from axiom.extensions.builtins.data_platform import kit
from axiom.extensions.builtins.data_platform.kit import host
from axiom.extensions.builtins.data_platform.kit.bundle import pack
from axiom.extensions.builtins.data_platform.kit.medallion import connect, dsn
from axiom.extensions.builtins.data_platform.kit.project import KitError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (shutil.which("docker") or os.environ.get("AXIOM_KIT_DSN")),
        reason="needs Docker or AXIOM_KIT_DSN",
    ),
]

SITE = "busy-loop"
SERVE = "serve_ro_test"


def _as(d: str, user: str, password: str) -> str:
    from psycopg.conninfo import make_conninfo

    return make_conninfo(d, user=user, password=password)


@pytest.fixture(scope="module")
def node(tmp_path_factory):
    """A medallion with the site's rows and a read-only serving login, never set up for kits."""
    repo = tmp_path_factory.mktemp("busy-node")
    kit.init(repo, SITE)
    p = kit.load(repo)
    kit.up(p)
    kit.try_kit(p)
    d = dsn(p)
    with connect(d) as conn:
        conn.execute("ALTER TABLE silver.signals DISABLE ROW LEVEL SECURITY")  # as a fresh node
        conn.execute(
            f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{SERVE}') "
            f"THEN CREATE ROLE {SERVE} LOGIN PASSWORD 'ro'; END IF; END $$"
        )
        conn.execute(f"GRANT USAGE ON SCHEMA silver, gold TO {SERVE}")
        conn.execute(f"GRANT SELECT ON silver.signals TO {SERVE}")
        conn.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA gold TO {SERVE}")
        conn.execute(f"DROP SCHEMA IF EXISTS {host.SCHEMA} CASCADE")
    yield p, d, _as(d, SERVE, "ro")
    kit.down(p)


def test_a_read_only_serving_login_submits_once_the_operator_has_set_up(node):
    p, d, ro = node
    bundle = pack(p).data
    with pytest.raises(host.RegistryNotReady, match="kit-host-setup"):
        host.submit(ro, bundle, caller_site=SITE, principal="@c:busy-loop", home=False)

    done = host.setup(d, serve_role=SERVE)
    assert "row-level security on silver.signals" in done["done"]
    assert any(x == f"reader policy for {SERVE}" for x in done["done"]), done

    r = host.submit(ro, bundle, caller_site=SITE, principal="@c:busy-loop", home=False)
    assert r["state"] == "checked", r["problems"]
    applied = host.approve(d, r["id"], approver="@operator:home")
    assert applied["state"] == "applied"
    assert host.list_verbs(ro, site=SITE), "the serving login reads the registry"
    rows = host.run_registered_verb(ro, site=SITE, name="temperature_rise_by_day")
    assert rows, "the serving login runs a promoted verb as the site's role"
    with connect(ro) as conn:
        assert conn.execute("SELECT count(*) FROM silver.signals").fetchone()[0] > 0, \
            "a reader from before the first kit still sees silver"
    with pytest.raises(Exception, match="permission denied"):
        with connect(ro) as conn:
            conn.execute(f"DELETE FROM {host.SCHEMA}.bundles")


def test_an_approve_behind_a_busy_table_stays_checked_and_a_later_one_takes_no_lock(node, monkeypatch):
    import psycopg

    p, d, _ = node
    host.setup(d, serve_role=SERVE)
    host.retire(d, SITE, by="@operator:home")
    with connect(d) as conn:  # the site's first apply has not happened yet
        conn.execute('DROP POLICY IF EXISTS "kit_busy_loop_rows" ON silver.signals')
    monkeypatch.setattr(host, "LOCK_TIMEOUT", "300ms")

    def busy():
        reader = psycopg.connect(d)
        reader.execute("LOCK TABLE silver.signals IN ACCESS SHARE MODE")  # a long read, still open
        return reader

    first = host.submit(d, pack(p).data, caller_site=SITE, principal="@c:busy-loop", home=False)
    reader = busy()
    try:
        with pytest.raises(KitError, match="still checked"):
            host.approve(d, first["id"], approver="@operator:home", attempts=2, backoff_s=0.05)
    finally:
        reader.close()
    (row,) = host.review(d, site=SITE, state=None)[:1]
    assert row["id"] == first["id"] and row["state"] == "checked", row
    assert "lock" in (row["last_error"] or ""), row

    assert host.approve(d, first["id"], approver="@operator:home")["state"] == "applied"

    second = host.submit(d, pack(p).data, caller_site=SITE, principal="@c:busy-loop", home=False)
    reader = busy()
    try:
        applied = host.approve(d, second["id"], approver="@operator:home", attempts=1)
    finally:
        reader.close()
    assert applied["state"] == "applied", "a re-approve needs no lock the readers hold"


def test_a_real_isolation_failure_still_rejects(node):
    p, d, _ = node
    host.setup(d, serve_role=SERVE)
    with connect(d) as conn:
        conn.execute(
            "CREATE OR REPLACE FUNCTION gold.busy_leak() RETURNS bigint "
            "LANGUAGE sql SECURITY DEFINER AS 'SELECT count(*) FROM silver.signals'"
        )
    try:
        r = host.submit(d, pack(p).data, caller_site=SITE, principal="@c:busy-loop", home=False)
        with pytest.raises(KitError, match="SECURITY DEFINER"):
            host.approve(d, r["id"], approver="@operator:home")
        assert [b for b in host.review(d, site=SITE, state="rejected") if b["id"] == r["id"]]
    finally:
        with connect(d) as conn:
            conn.execute("DROP FUNCTION IF EXISTS gold.busy_leak()")


# -- kit-try keeps what kit-materialize loaded ------------------------------------------


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    repo = tmp_path_factory.mktemp("pulled-kit")
    kit.init(repo, "pulled-loop")
    p = kit.load(repo)
    kit.up(p)
    yield p
    kit.down(p)


def test_the_intended_flow_runs_end_to_end_on_a_pulled_frame(sandbox):
    """kit-pull (here: its snapshot), kit-up, kit-materialize, kit-scaffold starter, kit-try."""
    p = sandbox
    rows = [
        {"site": "pulled-loop", "feed": "loop.main", "channel": ch, "unit": unit,
         "ts": f"2026-10-01T15:{m:02d}:00Z", "value": base + m}
        for ch, unit, base in (("Loop_T", "degC", 20.0), ("Valve_Pos", "percent", 40.0))
        for m in range(30)
    ]
    kit.take_snapshot(p, site="pulled-loop", channels=["Loop_T", "Valve_Pos"], rows=rows,
                      window={"start": "2026-10-01T15:00:00Z", "end": "2026-10-01T16:00:00Z"},
                      snapshot_id="oct1")
    from axiom.extensions.builtins.data_platform.kit.snapshot import materialize

    assert materialize(p, "oct1")["rows"] == 60
    kit.scaffold_contribution(p, "starter", "loop_view", from_snapshot="oct1")

    report = kit.try_kit(kit.load(p.root))
    drawn = {c["chart"]: c["rows"] for c in report["charts"]}
    assert drawn.get("loop_view_degc") and drawn.get("loop_view_percent"), drawn
    assert report["verbs"]["loop_view_by_hour"], "the starter's verb reads the pulled frame"
    with connect(dsn(p)) as conn:
        kept = conn.execute(
            "SELECT count(*) FROM silver.signals WHERE schema_ref = 'kit:snapshot'").fetchone()[0]
    assert kept == 60, "kit-try deleted the snapshot it was meant to run over"
    # and a second kit-try does not double the kit's own conformed rows
    again = kit.try_kit(kit.load(p.root))
    assert again["silver"]["rows_out"] == report["silver"]["rows_out"]
