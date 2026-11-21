# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``channels``, ``latest`` and ``show`` on a node that holds the medallion, against a real Postgres.

The fake node in the other tests answers what the grammar says it should. This one is the
real executor: the real filter compiler, the real gold views, the real catalogue table, and
the same skills composing the same reads. A throwaway container, never a developer's database.
"""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("psycopg2")

import psycopg  # noqa: E402

from axiom.extensions.builtins.data_platform import read_target as rt  # noqa: E402
from axiom.extensions.builtins.data_platform.conformance import (  # noqa: E402
    GOLD_SIGNALS_DDL,
    SILVER_DDL,
    apply_conformance_ddl,
    catalogue,
)
from axiom.extensions.builtins.data_platform.skills import friendly, gold  # noqa: E402
from axiom.infra.skills import SkillContext, SkillRegistry  # noqa: E402

from . import timescale_container  # noqa: E402

pytestmark = pytest.mark.integration

SITE = "example-loop"


@pytest.fixture(scope="module")
def dsn():
    name, url = timescale_container.start()
    with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
        apply_conformance_ddl(cur, SILVER_DDL + GOLD_SIGNALS_DDL)
        catalogue.ensure_schema(cur)
        for channel, base, role, unit in (("T1", 20, "fluid_temperature", "degC"),
                                          ("T2", 30, "fluid_temperature", "degC"),
                                          ("P1", 100, "line_pressure", "kPa")):
            cur.execute(
                """INSERT INTO silver.signals (site, feed, channel, ts, value, unit, schema_ref, row_hash, role)
                   SELECT %s, 'loop.main', %s, g, %s + (extract(epoch FROM g)::bigint / 600 %% 7), %s,
                          'x/v1', %s || g::text, %s
                   FROM generate_series('2026-03-10'::timestamptz, '2026-03-12'::timestamptz - interval '10 min',
                                        interval '10 min') AS g""",
                (SITE, channel, base, unit, channel, role),
            )
        cur.execute(
            """INSERT INTO silver.signals (site, feed, channel, ts, value, unit, schema_ref, row_hash)
               SELECT %s, 'loop.main', 'Old1', g, 5, 'degC', 'x/v1', 'Old1' || g::text
               FROM generate_series('2026-03-01'::timestamptz, '2026-03-01 05:00'::timestamptz, interval '10 min') g""",
            (SITE,),
        )
        catalogue.rebuild(cur, SITE)
    yield url
    timescale_container.stop(name)


@pytest.fixture
def node(dsn, monkeypatch):
    """This install holds the medallion: reads run against the database, not a binding."""
    monkeypatch.setenv("DP1_RAG_DSN", dsn)
    monkeypatch.setattr(gold, "_GUARD", None)
    monkeypatch.setattr(rt, "resolve_read_target", lambda: rt.ReadTarget("medallion"))
    monkeypatch.setattr(rt, "current_binding", lambda: None)


@pytest.fixture
def ctx(tmp_path):
    return SkillContext(registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("t"), surface="cli")


def test_channels_from_the_catalogue(node, ctx):
    result = friendly.channels({}, ctx)
    assert result.ok, result.errors
    by = {c["channel"]: c for c in result.value["data"]["channels"]}
    assert result.value["data"]["site"] == SITE  # the only site on the node
    assert (by["T1"]["unit"], by["T1"]["role"]) == ("degC", "fluid_temperature")
    assert by["T1"]["first_ts"] == "2026-03-10T00:00:00Z" and by["T1"]["last_ts"] == "2026-03-11T23:50:00Z"
    assert by["Old1"]["role"] is None


def test_latest_reads_each_channel_at_its_own_last_timestamp(node, ctx):
    result = friendly.latest({}, ctx)
    assert result.ok, result.errors
    by = {r["channel"]: r for r in result.value["data"]["readings"]}
    assert by["T1"]["ts"] == "2026-03-11T23:50:00Z" and by["T1"]["value"] is not None
    assert by["Old1"]["ts"] == "2026-03-01T05:00:00Z" and by["Old1"]["value"] == 5


def test_show_a_role_defaults_to_the_latest_day_with_readings(node, ctx):
    result = friendly.show({"target": "fluid_temperature"}, ctx)
    assert result.ok, result.errors
    data = result.value["data"]
    assert data["day"] == "2026-03-11" and data["bucket"] == "30 minutes" and data["channels"] == ["T1", "T2"]
    assert {p["channel"] for p in data["series"]} == {"T1", "T2"}
    assert len([p for p in data["series"] if p["channel"] == "T1"]) == 48


def test_show_a_channel_and_its_empty_window(node, ctx):
    ok = friendly.show({"target": "P1", "every": "6h"}, ctx)
    assert ok.ok, ok.errors
    assert ok.value["data"]["units"] == ["kPa"] and len(ok.value["data"]["series"]) == 4
    empty = friendly.show({"target": "P1", "day": "2026-04-01"}, ctx)
    assert not empty.ok
    assert "its last reading was 2026-03-11; try --day 2026-03-11" in empty.errors[0]


def test_unknown_names_are_answered_with_the_nearest_real_one(node, ctx):
    assert friendly.show({"target": "fluid_temp"}, ctx).errors == [
        f"no role or channel 'fluid_temp' at {SITE}; did you mean fluid_temperature?"]
    assert friendly.channels({"role": "line_presure"}, ctx).errors == [
        f"no role 'line_presure' at {SITE}; did you mean line_pressure?"]
