# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-265 step 1 (D9), the expand step: gold serves readings as ``gold.readings``.

"Signal" is retired. The gold views are the name a reader sees, so they are renamed first,
expand-first (ADR-182): this release adds ``gold.readings``, ``gold.readings_latest`` and
``gold.readings_uncertainty`` beside the old views, which stay for one release because a
client older than this one still asks for ``signals``. Both names answer the same rows,
and the new name finds its uncertainty companion by the same naming rule.
"""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("psycopg2")

import psycopg  # noqa: E402
import psycopg2  # noqa: E402

from axiom.extensions.builtins.data_platform import gold_query as gq  # noqa: E402
from axiom.extensions.builtins.data_platform.conformance import (  # noqa: E402
    GOLD_SIGNALS_DDL,
    SILVER_DDL,
    apply_conformance_ddl,
)

from . import timescale_container  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def cur():
    name, url = timescale_container.start()
    with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as c:
        apply_conformance_ddl(c, SILVER_DDL + GOLD_SIGNALS_DDL)
        c.execute(
            """INSERT INTO silver.signals (site, feed, channel, ts, value, unit, schema_ref, row_hash, uncertainty)
               SELECT 'site-a', 'loop.main', 'T1', g, 20, 'degC', 'x/v1', 'T1' || g::text, 0.5
               FROM generate_series('2026-03-10'::timestamptz, '2026-03-10 01:00'::timestamptz,
                                    interval '10 min') AS g"""
        )
    conn2 = psycopg2.connect(url)
    conn2.set_session(readonly=True, autocommit=True)
    with conn2.cursor() as c:
        yield c
    conn2.close()
    timescale_container.stop(name)


def test_the_new_views_exist_beside_the_old():
    created = " ".join(GOLD_SIGNALS_DDL)
    for view in ("gold.readings ", "gold.readings_latest", "gold.readings_uncertainty",
                 "gold.signals ", "gold.signals_latest", "gold.signals_uncertainty"):
        assert f"VIEW {view.strip()} " in created or f"VIEW {view.strip()}\n" in created


def test_both_names_answer_the_same_rows(cur):
    for view in ("readings", "signals"):
        cur.execute(f"SELECT count(*), max(value) FROM gold.{view}")  # noqa: S608 - fixed names
        assert cur.fetchone() == (7, 20)
    cur.execute("SELECT count(*) FROM gold.readings_latest")
    assert cur.fetchone() == (1,)


def test_a_served_read_by_the_new_name_keeps_its_uncertainty(cur):
    out = gq.aggregate(cur, table="readings", column="value", fn="max")
    assert out["data"]["value"] == 20
    assert out["uncertainty"]["low"] == 0.5
