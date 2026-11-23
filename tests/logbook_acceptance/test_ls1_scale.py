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

Sizes: the per-PR run seeds ``smoke_rows`` (10,000) and is bounded for a shared machine; the
nightly scale job sets ``AXIOM_SCALE_TESTS=1`` and ``AXIOM_SCALE_ROWS`` (default ``scale_rows``,
1,000,000) for the ``scale``-marked budgets. The limits are ``ls1_budgets.toml``; the rows are
the scale fixture's (``journal.scale_fixture``), fictional (D31).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.journal import scale_fixture, service, store  # noqa: E402

from . import ls1_budgets  # noqa: E402
from .ls1_budgets import SCALE_ROWS, SMOKE_ROWS, p95_ms  # noqa: E402
from .test_j1 import bus, caller, journal_db, live  # noqa: E402,F401

# ruff: noqa: F811  (pytest fixtures imported from the J1 tests)

ME = "@op1:site-a"
SRC = Path(__file__).resolve().parents[2] / "src" / "axiom" / "extensions" / "builtins"


def _seed(n: int) -> None:
    """The scale fixture's Timeline rows (``journal.scale_fixture``, what ``log seed`` writes)."""
    with store.session_scope() as s:
        scale_fixture.seed_events(s, site="site-a", n=n, me=ME)


def _budgets(n: int) -> dict[str, float]:
    me = caller(ME)
    first = service.query(me, limit=50)
    assert len(first["rows"]) == 50 and first["next_cursor"]
    mine = service.query(me, mine=True, limit=50)
    assert mine["rows"] and all(
        r["actor"] == ME or ME in (r["ref"].get("for") or []) for r in mine["rows"]
    )
    return {
        "timeline_first": p95_ms(lambda: service.query(me, limit=50)),
        "timeline_older": p95_ms(lambda: service.query(me, limit=50, cursor=str(n // 2))),
        "timeline_mine": p95_ms(lambda: service.query(me, mine=True, limit=50)),
        "timeline_agent": p95_ms(lambda: service.query(me, agent="tidy", limit=50)),
        "timeline_level": p95_ms(lambda: service.query(me, levels="bad", lanes="platform", limit=50)),
        "tail_forward": p95_ms(lambda: service.after(me, after=n - 500, limit=200)),
    }


@pytest.mark.acceptance("LB-LS1-A1", "LB-LS1-A2")
def test_lb_ls1_the_timeline_and_tail_answer_within_budget_smoke(live):
    _seed(SMOKE_ROWS)
    over = ls1_budgets.over(_budgets(SMOKE_ROWS))
    assert not over, f"p95 at or over budget at {SMOKE_ROWS} rows: {over}"


@pytest.mark.scale
@pytest.mark.acceptance("LB-LS1-A1", "LB-LS1-A2")
def test_lb_ls1_the_timeline_and_tail_answer_within_budget_at_scale(live):
    if os.environ.get("AXIOM_SCALE_TESTS") != "1":
        pytest.skip("scale budgets run in the nightly scale job (AXIOM_SCALE_TESTS=1)")
    _seed(SCALE_ROWS)
    numbers = _budgets(SCALE_ROWS)
    ls1_budgets.record(numbers, rows=SCALE_ROWS)
    over = ls1_budgets.over(numbers)
    assert not over, f"p95 at or over budget at {SCALE_ROWS} rows: {over}"


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
    SRC.parents[1] / "attest" / "verify.py",
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
                found.append(f"{path.relative_to(SRC.parents[1])}:{no}: {line.strip()}")
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
