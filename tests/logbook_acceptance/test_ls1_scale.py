# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LS-1 (#1780): the Timeline and the Tail at scale, and a guard against full reads per page.

The journal's reads are keyset pages on ``seq`` with every filter in SQL. These budgets hold that
true as the index grows:

- LB-LS1-A1: the Timeline's first page, a Mine page, an agent's Activity page and an older page
  answer within the first-page budget (p95 under 200 ms, server side);
- LB-LS1-A2: the Tail's forward page (``after``) answers within the same budget;
- LB-LS1-A3: no read path of the Log or the journal pages with ``OFFSET`` or slices a whole
  result in Python (a static guard over the source, cheap, on every pull request).

Sizes: the per-PR run seeds ``SMOKE_ROWS`` (10,000) and is bounded for a shared machine; the
nightly scale job sets ``AXIOM_SCALE_TESTS=1`` and ``AXIOM_SCALE_ROWS`` (default 1,000,000) for
the ``scale``-marked budgets. Fictional rows only (D31): invented actors, lanes and subjects.
"""

from __future__ import annotations

import os
import re
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from sqlalchemy import text  # noqa: E402

from axiom.extensions.builtins.journal import service, store, writer  # noqa: E402

from .test_j1 import bus, caller, journal_db, live  # noqa: E402,F401

# ruff: noqa: F811  (pytest fixtures imported from the J1 tests)

SMOKE_ROWS = 10_000
SCALE_ROWS = int(os.environ.get("AXIOM_SCALE_ROWS", "1000000"))
FIRST_PAGE_BUDGET_MS = 200.0
ME = "@op1:site-a"
SRC = Path(__file__).resolve().parents[2] / "src" / "axiom" / "extensions" / "builtins"

# One month, so one partition; every eleventh row is the caller's, every 97th done for them by an
# agent, every 13th an agent's; lanes and statuses rotate.
SEED = """
INSERT INTO journal_events (at, id, recorded_at, lane, subject, source, source_id, status, actor,
                            summary, subjects, ref, link, site, logbook, classification,
                            retention_class)
SELECT
    timestamp with time zone '2026-01-01 00:00:00+00' + (g * interval '1 second'),
    md5('row:' || g::text)::uuid,
    now(),
    (ARRAY['platform', 'commands', 'agents', 'tool_audit'])[1 + g % 4],
    'scale.thing',
    'scale',
    'scale:' || g::text,
    (ARRAY['info', 'ok', 'warn', 'bad'])[1 + g % 4],
    CASE WHEN g % 11 = 0 THEN :me WHEN g % 13 = 0 THEN '@tidy' ELSE '@op' || (g % 50)::text || :op_site END,
    'row ' || g::text,
    '[]'::jsonb,
    CASE WHEN g % 97 = 0 THEN jsonb_build_object('for', jsonb_build_array(:me)) ELSE '{}'::jsonb END,
    NULL,
    'site-a',
    NULL,
    'internal',
    'month'
FROM generate_series(1, :n) AS g
"""


def _seed(n: int) -> None:
    with store.session_scope() as s:
        writer.ensure_partition(s, datetime(2026, 1, 15, tzinfo=UTC))
        s.execute(text(SEED), {"n": n, "me": ME, "op_site": ":site-a"})
        s.execute(text("ANALYZE journal_events"))
        s.commit()


def _p95(samples: list[float]) -> float:
    return statistics.quantiles(samples, n=20)[-1] if len(samples) >= 20 else max(samples)


def _time(call, runs: int = 20) -> float:
    call()  # warm the plan and the connection
    samples = []
    for _ in range(runs):
        t0 = time.perf_counter()
        call()
        samples.append((time.perf_counter() - t0) * 1000)
    return _p95(samples)


def _budgets(n: int) -> dict[str, float]:
    me = caller(ME)
    first = service.query(me, limit=50)
    assert len(first["rows"]) == 50 and first["next_cursor"]
    mine = service.query(me, mine=True, limit=50)
    assert mine["rows"] and all(
        r["actor"] == ME or ME in (r["ref"].get("for") or []) for r in mine["rows"]
    )
    return {
        "first": _time(lambda: service.query(me, limit=50)),
        "older": _time(lambda: service.query(me, limit=50, cursor=str(n // 2))),
        "mine": _time(lambda: service.query(me, mine=True, limit=50)),
        "agent": _time(lambda: service.query(me, agent="tidy", limit=50)),
        "level": _time(lambda: service.query(me, levels="bad", lanes="platform", limit=50)),
        "tail": _time(lambda: service.after(me, after=n - 500, limit=200)),
    }


def _assert_within(numbers: dict[str, float], n: int) -> None:
    over = {k: round(v) for k, v in numbers.items() if v >= FIRST_PAGE_BUDGET_MS}
    assert not over, f"p95 over {FIRST_PAGE_BUDGET_MS:.0f} ms at {n} rows: {over}"


@pytest.mark.acceptance("LB-LS1-A1", "LB-LS1-A2")
def test_lb_ls1_the_timeline_and_tail_answer_within_budget_smoke(live):
    _seed(SMOKE_ROWS)
    _assert_within(_budgets(SMOKE_ROWS), SMOKE_ROWS)


@pytest.mark.scale
@pytest.mark.acceptance("LB-LS1-A1", "LB-LS1-A2")
def test_lb_ls1_the_timeline_and_tail_answer_within_budget_at_scale(live):
    if os.environ.get("AXIOM_SCALE_TESTS") != "1":
        pytest.skip("scale budgets run in the nightly scale job (AXIOM_SCALE_TESTS=1)")
    _seed(SCALE_ROWS)
    numbers = _budgets(SCALE_ROWS)
    print(f"LS-1 journal p95 ms at {SCALE_ROWS} rows: {numbers}")  # the nightly's summary line
    _assert_within(numbers, SCALE_ROWS)


# -- A3: the static guard -------------------------------------------------------------------

READ_PATHS = [
    SRC / "attest" / "api.py",
    SRC / "attest" / "service.py",
    SRC / "attest" / "filters.py",
    SRC / "attest" / "skills" / "read.py",
    SRC / "journal" / "api.py",
    SRC / "journal" / "service.py",
    SRC / "journal" / "follow.py",
    SRC / "journal" / "skills" / "read.py",
]
#: ``OFFSET`` in SQL or ``.offset(`` in a query builder.
OFFSET = re.compile(r"\bOFFSET\b|\.offset\(")
#: A call's whole result sliced in Python: ``something(...)[-n:]`` or ``.all()[:n]``.
SLICED = re.compile(r"\)\s*\[\s*(?:-\s*[A-Za-z_]\w*\s*:|:\s*[A-Za-z_]\w*)\s*\]")


@pytest.mark.acceptance("LB-LS1-A3")
def test_lb_ls1_no_read_path_pages_with_offset_or_slices_a_whole_result():
    found = []
    for path in READ_PATHS:
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if OFFSET.search(code) or SLICED.search(code):
                found.append(f"{path.relative_to(SRC)}:{no}: {line.strip()}")
    assert not found, (
        "read a page in SQL (keyset on seq, LIMIT), not a whole result:\n" + "\n".join(found)
    )


def test_the_guard_catches_what_it_is_for():
    assert SLICED.search("rows = service.unacknowledged(site, log)[-n:]")
    assert SLICED.search("x = s.execute(q).all()[:limit]")
    assert OFFSET.search("SELECT * FROM t OFFSET :o")
    assert not SLICED.search("has_more, page = len(page) > n, page[-n:]")
    assert not SLICED.search("return f'W/\"{h.hexdigest()[:32]}\"'")


@pytest.mark.acceptance("LB-LS1-A3")
def test_lb_ls1_the_newest_unacknowledged_and_cosign_pages_are_bounded_reads(
    attest_db, node_signer
):
    """The two lists the guard caught read their newest page in SQL and return what the old
    read-everything-then-slice returned."""
    from axiom.extensions.builtins.attest import service as attest

    from .test_l2 import original

    for i in range(7):
        original(node_signer, reading=f"r{i}")
    everything = attest.unacknowledged("site-a", "demo_log")
    assert attest.unacknowledged("site-a", "demo_log", newest=3) == everything[-3:]
    assert attest.unacknowledged("site-a", "demo_log", newest=50) == everything
    assert (
        attest.pending_cosigns("site-a", "demo_log", newest=2)
        == (attest.pending_cosigns("site-a", "demo_log")[-2:])
    )
