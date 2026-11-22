# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Budgets for the Log at scale (unit W3 and LS-1, #1780): a site with a million entries across
twenty logs still answers a log's newest page, an older page, a filtered page and a search within
budget, because the page is a keyset read from the indexes and the text filter is the generated
search projection; ``verify`` of a 10,000-entry slice reads that slice only; and the export of a
100,000-entry log streams: its first bytes within a second, its evidence package written in a
time the nightly job tracks and in memory that does not grow with the log (#2182).

These run only when ``AXIOM_SCALE_TESTS=1`` (the nightly scale job): seeding a million rows takes
a minute, and the budgets mean nothing on a loaded laptop. ``AXIOM_SCALE_ROWS=10000`` runs the same
test small, for a local check. The rows are the scale fixture's (``attest.scale_fixture``, what
``log seed --scale`` writes): fictional (D31), with unique linked digests that are not signed, since
these budgets are about reading; the ``verify`` budget signs a chain of its own. The limits live in
``ls1_budgets.toml`` and the measured numbers are published by ``ls1_budgets.record``.
"""

from __future__ import annotations

import os
import tempfile
import time
import tracemalloc
from urllib.parse import quote

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import exports, registry, scale_fixture, store  # noqa: E402
from axiom.extensions.builtins.attest.logbooks import parse_logbook  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, as_  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import SITE  # noqa: E402
from axiom.extensions.builtins.search.skills import run as search_run  # noqa: E402

from . import ls1_budgets  # noqa: E402
from .ls1_budgets import SCALE_ROWS, p95_ms  # noqa: E402

pytestmark = pytest.mark.scale

LOG = "scale_log_01"
#: The first entry carrying the rare search needle is row ``NEEDLE_EVERY``, in this log.
NEEDLE_LOG = scale_fixture.log_ids()[(scale_fixture.NEEDLE_EVERY - 1) % scale_fixture.LOGBOOKS]
VERIFY_SLICE = 10_000
EXPORT_ROWS = 100_000
#: A streamed export's peak Python memory, whatever the log's size (#2182; whole, it was 449 MiB).
EXPORT_PEAK_MIB = 64


@pytest.fixture(scope="module")
def rows(request):
    if os.environ.get("AXIOM_SCALE_TESTS") != "1":
        pytest.skip("scale budgets run in the nightly scale job (AXIOM_SCALE_TESTS=1)")
    return SCALE_ROWS


@pytest.fixture
def big(client, rows):
    scale_fixture.register()
    with store.session_scope() as s:
        scale_fixture.seed_records(s, site=SITE, n=rows)
    return as_(client, OP1)


def _ok(r):
    assert r.status_code == 200, r.text


@pytest.mark.acceptance("LB-W3-A1", "LB-LS1-A9")
def test_the_records_door_verify_and_export_answer_within_budget_at_scale(big, rows, node_signer):
    c = big
    per_log = -(-rows // scale_fixture.LOGBOOKS)
    records = f"/api/v1/attest/{LOG}/records?limit=200"
    page = c.get(records).json()
    assert page["page"]["newest_seq"] == per_log and len(page["records"]) == 200
    signer = quote(f"@desk3:{SITE}")
    session = scale_fixture.SESSION_SEED + (per_log // 2) // scale_fixture.SESSION_LENGTH
    searched = f"{records}&entry_type=NOTE&signer={signer}&q=handover"
    hits = c.get(searched).json()["records"]
    assert hits and all("handover" in r["title"] and r["entry_type"] == "NOTE" for r in hits)

    numbers = {
        "log_first_page": p95_ms(lambda: c.get(records), check=_ok),
        "log_older_page": p95_ms(lambda: c.get(f"{records}&before_seq={per_log // 2}"), check=_ok),
        "log_filtered_page": p95_ms(lambda: c.get(f"{records}&entry_type=CHECK&signer={signer}"), check=_ok),
        "log_interval_page": p95_ms(lambda: c.get(f"{records}&interval=session:{session}"), check=_ok),
        "log_search": p95_ms(lambda: c.get(searched), check=_ok),
    }  # fmt: skip

    # The node search narrowed to one log reads the same projection, through its skill.
    def node_search():
        return search_run({"query": scale_fixture.NEEDLE, "kinds": ["logbook"], "logbook": NEEDLE_LOG,
                           "_sites": [SITE], "limit": 20}, None)  # fmt: skip

    def found(result):
        assert result.ok, result.errors
        if rows >= scale_fixture.NEEDLE_EVERY:
            assert result.value["results"], "the needle is in the fixture at this size"

    numbers["node_search"] = p95_ms(node_search, check=found)

    # verify a slice of a really signed chain, the one record before it read as its anchor
    from axiom.attest import verify as verify_lib

    keys = {node_signer.key_id: node_signer.public_bytes}
    with store.session_scope() as s:
        scale_fixture.signed_chain(s, site=SITE, logbook="scale_signed", n=VERIFY_SLICE + 1,
                                   signer=node_signer)  # fmt: skip

    def verified(report):
        assert report.ok and report.checked == VERIFY_SLICE, report

    numbers["verify_slice_10k"] = p95_ms(
        lambda: verify_lib.chain(SITE, "scale_signed", 2, VERIFY_SLICE + 1, public_keys=keys),
        runs=5, check=verified,
    )

    # the export of a 100,000-entry log streams (#2182): the first bytes of its records view, and
    # the evidence package written to a file a batch at a time, in memory that does not grow
    registry.register(parse_logbook(scale_fixture.declaration("scale_log_export"), source="ls1"))
    with store.session_scope() as s:
        scale_fixture.seed_records(s, site=SITE, n=EXPORT_ROWS, logbooks=["scale_log_export"])
    everything = exports.parse_filter({})

    def first_bytes():
        stream = exports.stream_view(SITE, "scale_log_export", everything, fmt="jsonl")
        try:
            return next(stream)
        finally:
            stream.close()

    def some(chunk):
        assert chunk.startswith(b"{"), chunk[:80]

    numbers["export_100k_first_bytes"] = p95_ms(first_bytes, runs=5, check=some)
    tracemalloc.start()
    t0 = time.perf_counter()
    with tempfile.TemporaryFile() as fh:
        pkg = exports.write_package(SITE, "scale_log_export", everything, formats=["jsonl"], out=fh)
    numbers["export_100k_build"] = (time.perf_counter() - t0) * 1000
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert pkg.manifest["counts"]["chain"] == EXPORT_ROWS
    print(f"LS-1 export of {EXPORT_ROWS} entries: peak Python memory {peak / 2**20:.0f} MiB")
    assert peak < EXPORT_PEAK_MIB * 2**20, f"the streamed export peaked at {peak / 2**20:.0f} MiB"

    ls1_budgets.record(numbers, rows=rows)
    over = ls1_budgets.over(numbers)
    assert not over, f"p95 at or over budget at {rows} entries: {over}"
