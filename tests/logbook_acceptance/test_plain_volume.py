# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A plain log at volume (ADR-290): many one-step entries keep one valid chain, and the ordinary
reads (newest page, an older page, a text search) stay fast. ``AXIOM_PLAIN_ROWS`` sets the size
(default 300, quick; a node check runs thousands). The numbers are printed for the record; the
limits are loose on purpose, since they guard against a read that scans the log, not a slow disk.
"""

from __future__ import annotations

import os
import statistics
import time

import pytest

pytest.importorskip("fastapi")

from axiom.attest import verify as verify_lib  # noqa: E402
from axiom.extensions.builtins.attest import registry, service  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, as_  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import SITE, person  # noqa: E402

from .test_plain import (
    parse,  # noqa: E402
    roles,  # noqa: E402,F401
)

ROWS = int(os.environ.get("AXIOM_PLAIN_ROWS", "300"))
LOG = "plain_volume"
WORDS = ("handover", "inspection", "delivery", "calibration", "visitor", "cleanup")


def p95(samples):
    return statistics.quantiles(samples, n=20)[-1] if len(samples) >= 20 else max(samples)


@pytest.mark.acceptance("LB-PL-A18")
@pytest.mark.usefixtures("roles")
def test_pl_a18_a_plain_log_at_volume_keeps_one_valid_chain_and_fast_reads(
    attest_db, bound_signer, client, monkeypatch
):
    monkeypatch.setenv("AXIOM_SITE", SITE)
    registry.register(parse(LOG))
    who = person()
    t0 = time.perf_counter()
    for i in range(ROWS):
        word = WORDS[i % len(WORDS)]
        service.record_entry(
            site_id=SITE, logbook=LOG, entry_type="NOTE", by=who,
            content={"title": f"{word} {i}", "fields": {"text": f"{word} note number {i}"}},
        )  # fmt: skip
    per_write = (time.perf_counter() - t0) * 1000 / ROWS

    keys = {bound_signer.key_id: bound_signer.public_bytes}
    t0 = time.perf_counter()
    report = verify_lib.chain(SITE, LOG, 1, ROWS, public_keys=keys)
    verify_ms = (time.perf_counter() - t0) * 1000
    assert report.ok and report.checked == ROWS, report

    c = as_(client, OP1)
    base = f"/api/v1/attest/{LOG}/records?limit=50"

    def timed(url):
        samples = []
        for _ in range(21):
            t = time.perf_counter()
            r = c.get(url)
            samples.append((time.perf_counter() - t) * 1000)
            assert r.status_code == 200, r.text
        return p95(samples[1:]), r.json()

    first_ms, first = timed(base)
    older_ms, _ = timed(f"{base}&before_seq={max(ROWS // 2, 2)}")
    search_ms, found = timed(f"{base}&q=calibration")
    assert len(first["records"]) == min(50, ROWS)
    assert found["records"] and all("calibration" in r["title"] for r in found["records"])
    print(f"PLAIN-LOG rows={ROWS} write_ms={per_write:.2f} verify_ms={verify_ms:.0f} "
          f"first_p95={first_ms:.1f} older_p95={older_ms:.1f} search_p95={search_ms:.1f}")  # fmt: skip
    # loose on purpose: a shared runner is slow, and what this guards is a read that scans the log
    assert per_write < 250 and max(first_ms, older_ms, search_ms) < 1000
