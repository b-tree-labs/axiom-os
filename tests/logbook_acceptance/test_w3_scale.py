# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Budgets for the Log at scale (unit W3; owner requirement 2026-11-11): a logbook with a million
entries at one site still answers its newest page and a text search within budget, because the
page is a keyset read from the indexes and the text filter is the generated search projection.

These run only when ``AXIOM_SCALE_TESTS=1`` (the nightly scale job): seeding a million rows takes
minutes, and the budgets are meaningless on a loaded laptop. The fixture is fictional (D31): an
invented site, invented signers, a synthetic chain whose digests are unique but not verified; the
budgets are about reading, not signing. Verification of a slice is covered in ``test_w3.py``.
"""

from __future__ import annotations

import os
import statistics
import time

import pytest

pytest.importorskip("fastapi")

from sqlalchemy import text  # noqa: E402

from axiom.extensions.builtins.attest import registry, store  # noqa: E402
from axiom.extensions.builtins.attest.logbooks import parse_logbook  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, as_  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import SITE  # noqa: E402
from axiom.extensions.builtins.search.skills import run as search_run  # noqa: E402

pytestmark = pytest.mark.scale

ROWS = int(os.environ.get("AXIOM_SCALE_ROWS", "1000000"))
FIRST_PAGE_BUDGET_MS = 200.0
SEARCH_BUDGET_MS = 300.0

BIG_LOG = {
    "logbook": {"id": "big_log", "version": "1", "display": "Big Log"},
    "type": [
        {"id": "NOTE", "meanings": ["authored"], "roles": ["operator"]},
        {"id": "CHECK", "meanings": ["performed"], "roles": ["operator"]},
    ],
}

SEED = """
INSERT INTO attest_records (attestation_id, site_id, logbook, seq, digest, prev_digest, kind, meaning,
                            entry_type, signer, occurred_at, recorded_at, record)
SELECT
    md5('id:' || g::text)::uuid::text,
    :site, :logbook, g,
    md5('digest:' || g::text) || md5('d2:' || g::text),
    md5('digest:' || (g - 1)::text) || md5('d2:' || (g - 1)::text),
    'statement',
    CASE WHEN g % 3 = 0 THEN 'performed' ELSE 'authored' END,
    CASE WHEN g % 3 = 0 THEN 'CHECK' ELSE 'NOTE' END,
    '@op' || (g % 7)::text || ':' || :site,
    timestamp with time zone '2025-01-01 00:00:00+00' + (g * interval '30 seconds'),
    timestamp with time zone '2025-01-01 00:00:00+00' + (g * interval '30 seconds'),
    jsonb_build_object(
        'attestation_id', md5('id:' || g::text)::uuid::text, 'seq', g, 'logbook', :logbook,
        'site_id', :site, 'kind', 'statement',
        'meaning', CASE WHEN g % 3 = 0 THEN 'performed' ELSE 'authored' END,
        'entry_type', CASE WHEN g % 3 = 0 THEN 'CHECK' ELSE 'NOTE' END,
        'signer', jsonb_build_object('principal', '@op' || (g % 7)::text || ':' || :site),
        'occurred_at', to_char(timestamp with time zone '2025-01-01 00:00:00+00' + (g * interval '30 seconds'),
                               'YYYY-MM-DD"T"HH24:MI:SS"+00:00"'),
        'digest', md5('digest:' || g::text) || md5('d2:' || g::text),
        'content', jsonb_build_object('title', 'entry ' || g::text || CASE WHEN g % 9973 = 0 THEN ' valve seat inspected' ELSE '' END,
                                      'fields', jsonb_build_object('reading', (g % 100)::text))
    )
FROM generate_series(1, :n) AS g
"""


@pytest.fixture(scope="module")
def million(request):
    if os.environ.get("AXIOM_SCALE_TESTS") != "1":
        pytest.skip("scale budgets run in the nightly scale job (AXIOM_SCALE_TESTS=1)")
    return ROWS


@pytest.fixture
def big(client, million):
    registry.register(parse_logbook(BIG_LOG, source="test:w3-scale"))
    with store.session_scope() as s:
        s.execute(text(SEED), {"site": SITE, "logbook": "big_log", "n": million})
        s.execute(text("ANALYZE attest_records"))
        s.commit()
    return as_(client, OP1)


def _p95(samples: list[float]) -> float:
    return statistics.quantiles(samples, n=20)[-1] if len(samples) >= 20 else max(samples)


def _time(call, runs: int = 25) -> float:
    call()  # warm the plan and the connection
    samples = []
    for _ in range(runs):
        t0 = time.perf_counter()
        r = call()
        samples.append((time.perf_counter() - t0) * 1000)
        assert r.status_code == 200, r.text
    return _p95(samples)


@pytest.mark.acceptance("LB-W3-A1")
def test_the_newest_page_of_a_million_entries_answers_within_budget(big, million):
    c = big
    p95 = _time(lambda: c.get("/api/v1/attest/big_log/records?limit=200"))
    page = c.get("/api/v1/attest/big_log/records?limit=200").json()
    assert page["page"]["newest_seq"] == million and len(page["records"]) == 200
    assert p95 < FIRST_PAGE_BUDGET_MS, f"first page p95 {p95:.0f} ms over {million} entries"
    older = _time(
        lambda: c.get(f"/api/v1/attest/big_log/records?limit=200&before_seq={million // 2}")
    )
    assert older < FIRST_PAGE_BUDGET_MS, f"an older page p95 {older:.0f} ms"
    filtered = _time(
        lambda: c.get(
            "/api/v1/attest/big_log/records?limit=200&entry_type=CHECK&signer=%40op3%3Asite-a"
        )
    )
    assert filtered < FIRST_PAGE_BUDGET_MS, f"a filtered page p95 {filtered:.0f} ms"


@pytest.mark.acceptance("LB-W3-A1")
def test_a_text_search_over_a_million_entries_answers_within_budget(big):
    c = big
    p95 = _time(lambda: c.get("/api/v1/attest/big_log/records?limit=200&q=valve%20seat"))
    hits = c.get("/api/v1/attest/big_log/records?limit=200&q=valve%20seat").json()["records"]
    assert hits and all("valve seat" in (h["title"] or "") for h in hits)
    assert p95 < SEARCH_BUDGET_MS, f"search p95 {p95:.0f} ms"
    # The node search narrowed to the log reads the same projection, through its skill.
    samples = []
    for _ in range(20):
        t0 = time.perf_counter()
        result = search_run(
            {
                "query": "valve seat",
                "kinds": ["logbook"],
                "logbook": "big_log",
                "_sites": [SITE],
                "limit": 20,
            },
            None,
        )
        samples.append((time.perf_counter() - t0) * 1000)
        assert result.ok and result.value["results"], result.errors
    assert _p95(samples) < SEARCH_BUDGET_MS, f"node search p95 {_p95(samples):.0f} ms"
