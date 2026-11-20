# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""S9 Search, filter and paging (#1818): over 10,000 seeded rows plus the run's own, each filter
selects exactly what a reference over the chain selects, keyset pages never repeat or skip while
new rows are signed between them, an unchanged page answers 304, and the run's own slice verifies.

The rows are seeded in ONE statement (``generate_series``), never a loop of inserts, so the stage
stays a few seconds on a laptop. Budgets here are reported, not enforced; the nightly job holds
them at a million (#1780).
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest

from .conftest import ANN, LOG, SITE, as_, sign_cli

ROWS = int(os.environ.get("AXIOM_E2E_ROWS", "10000"))

SEED = """
INSERT INTO attest_records (attestation_id, site_id, logbook, seq, digest, prev_digest, kind, meaning,
                            entry_type, signer, occurred_at, recorded_at, record)
SELECT md5('e2e:' || g::text)::uuid::text, :site, :logbook, g,
       md5('d:' || g::text) || md5('e:' || g::text),
       md5('d:' || (g - 1)::text) || md5('e:' || (g - 1)::text),
       'statement', 'authored',
       CASE WHEN g % 4 = 0 THEN 'EXPLAIN' ELSE 'NOTE' END,
       CASE WHEN g % 5 = 0 THEN :ann ELSE :bo END,
       timestamp with time zone '2025-01-01 00:00:00+00' + (g * interval '1 minute'),
       timestamp with time zone '2025-01-01 00:00:00+00' + (g * interval '1 minute'),
       jsonb_build_object(
         'attestation_id', md5('e2e:' || g::text)::uuid::text, 'seq', g, 'logbook', :logbook,
         'site_id', :site, 'kind', 'statement', 'meaning', 'authored',
         'entry_type', CASE WHEN g % 4 = 0 THEN 'EXPLAIN' ELSE 'NOTE' END,
         'signer', jsonb_build_object('principal', CASE WHEN g % 5 = 0 THEN :ann ELSE :bo END),
         'occurred_at', to_char(timestamp with time zone '2025-01-01 00:00:00+00' + (g * interval '1 minute'),
                                'YYYY-MM-DD"T"HH24:MI:SS"+00:00"'),
         'digest', md5('d:' || g::text) || md5('e:' || g::text),
         'content', jsonb_build_object('title', 'entry ' || g::text
                                       || CASE WHEN g % 997 = 0 THEN ' desk lamp replaced' ELSE '' END,
                                       'fields', jsonb_build_object('text', 'row ' || g::text)))
FROM generate_series(1, :n) AS g
"""


@pytest.fixture
def bulk(world):
    from sqlalchemy import text

    from axiom.extensions.builtins.attest import store

    from .conftest import BO

    t0 = time.monotonic()
    with store.session_scope() as s:
        s.execute(
            text(SEED),
            {"site": SITE, "logbook": LOG, "n": ROWS, "ann": ANN.handle, "bo": BO.handle},
        )
        s.execute(
            text("INSERT INTO attest_chain_heads (site_id, logbook, seq, digest) VALUES (:site, :logbook, :n, "
                 "md5('d:' || CAST(:n AS text)) || md5('e:' || CAST(:n AS text)))"),
            {"site": SITE, "logbook": LOG, "n": ROWS},
        )  # fmt: skip
        s.execute(text("ANALYZE attest_records"))
        s.commit()
    seconds = time.monotonic() - t0
    print(f"\nseeded {ROWS} rows in {seconds:.2f} s")
    return ROWS


def _page(client, **params):
    r = client.get(f"/api/v1/attest/{LOG}/records", params={"limit": 200, **params})
    assert r.status_code == 200, r.text
    return r


@pytest.mark.acceptance("LB-E2E-A19")
def test_lb_e2e_a19_filters_and_keyset_pages_agree_with_a_reference_over_10000_rows(
    world, web, bulk
):
    from axiom.extensions.builtins.attest import service

    own = [sign_cli(ANN, "NOTE", {"text": "the run's own"}) for _ in range(2)]
    c = as_(web, ANN)
    chain = service.records(SITE, LOG)
    assert len(chain) == bulk + 2
    # each filter, and two together, against a reference over the chain
    for params, keep in (
        ({"entry_type": "EXPLAIN"}, lambda r: r.get("entry_type") == "EXPLAIN"),
        ({"signer": ANN.handle}, lambda r: r["signer"]["principal"] == ANN.handle),
        ({"entry_type": "EXPLAIN", "signer": ANN.handle},
         lambda r: r.get("entry_type") == "EXPLAIN" and r["signer"]["principal"] == ANN.handle),
        ({"q": "lamp"}, lambda r: "lamp" in (r.get("content") or {}).get("title", "")),
    ):  # fmt: skip
        want = [r["seq"] for r in chain if keep(r)][-200:]
        assert [r["seq"] for r in _page(c, **params).json()["records"]] == want, params
    # keyset paging backwards while a new row is signed between pages: nothing repeats or skips
    t0 = time.monotonic()
    first = _page(c).json()
    first_ms = (time.monotonic() - t0) * 1000
    seen = [r["seq"] for r in first["records"]]
    newest = sign_cli(ANN, "NOTE", {"text": "signed between pages"})
    page = first
    for _ in range(3):
        page = _page(c, before_seq=page["page"]["oldest_seq"]).json()
        seen += [r["seq"] for r in page["records"]]
    assert len(seen) == len(set(seen)) == 800 and max(seen) == own[-1]["seq"]
    newer = _page(c, after_seq=own[-1]["seq"]).json()
    assert [r["seq"] for r in newer["records"]] == [newest["seq"]]
    # an unchanged page answers 304 to its own ETag
    etag = _page(c, before_seq=5000).headers["etag"]
    assert c.get(f"/api/v1/attest/{LOG}/records", params={"limit": 200, "before_seq": 5000},
                 headers={"If-None-Match": etag}).status_code == 304  # fmt: skip
    # the run's own slice verifies (the seeded rows are synthetic and carry no signature)
    v = c.post(
        f"/api/v1/attest/{LOG}/verify", params={"from_seq": own[0]["seq"], "to_seq": newest["seq"]}
    ).json()
    assert v["ok"] and v["checked"] == 3
    print(f"first page {first_ms:.0f} ms over {bulk} rows (reported, not enforced; #1780 enforces)")


READ_PATHS = (
    "src/axiom/attest",
    "src/axiom/extensions/builtins/attest",
    "src/axiom/extensions/builtins/journal",
)


@pytest.mark.acceptance("LB-E2E-A20")
def test_lb_e2e_a20_no_attest_or_journal_read_path_pages_with_offset():
    root = Path(__file__).resolve().parents[2]
    offenders = []
    for base in READ_PATHS:
        for path in (root / base).rglob("*.py"):
            if "/tests/" in path.as_posix():
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"\bOFFSET\b|\.offset\(", line):
                    offenders.append(f"{path.relative_to(root)}:{n}")
    assert offenders == [], "keyset paging only (#1780): " + ", ".join(offenders)
