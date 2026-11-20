# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit W3, the Axiom half: search and filter a logbook, and an intervals
listing that shows the gaps.

Ticket: docs/working/logbook-units/W3.md in the product repo that holds the plan. The Log page
(axiom-appkit) adds a filter bar, a search box and an Intervals table; these rows pin the read
side it needs from Axiom: record filters on ``GET /{logbook}/records`` and on ``attest.show``,
``interval`` on the record brief (lists and the live stream), ``GET /{logbook}/intervals`` with
every interval's times, counts and the gaps between them (computed on the server, never from a
browser clock), ``intervals`` on the logbook JSON, the ``attest.intervals`` skill, and a
``logbook`` narrowing on ``search.query``. Every read stays inside the asker's site scope.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import intervals, obligations, registry  # noqa: E402
from axiom.extensions.builtins.attest.logbooks import parse_logbook  # noqa: E402
from axiom.extensions.builtins.attest.skills import SPECS  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, as_, sign_round  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import SITE  # noqa: E402
from axiom.extensions.builtins.search import engine  # noqa: E402
from axiom.extensions.builtins.search.skills import run as search_run  # noqa: E402

RUN_LOG = {
    "logbook": {"id": "run_log", "version": "1", "display": "Run Log"},
    "interval": {"run": {"opens": ["START"], "closes": ["STOP"]}},
    "type": [
        {"id": "START", "meanings": ["authored"], "roles": ["operator"], "requires_interval": "none_open"},
        {"id": "CHECK", "meanings": ["performed"], "roles": ["operator"], "requires_interval": "run"},
        {"id": "STOP", "meanings": ["authored"], "roles": ["operator"], "requires_interval": "run"},
        {"id": "NOTE", "meanings": ["authored"], "roles": ["operator"]},
    ],
    "obligation": [
        {
            "id": "check_cadence",
            "type": "CHECK",
            "every": {"site_key": "run.checks.interval_minutes", "default": 30},
            "warn_before": {"site_key": "run.checks.warn_lead_minutes", "default": 5},
            "while": "interval.run.open",
            "notify": ["operator"],
            "required": True,
        }
    ],
}  # fmt: skip

MEANING = {"START": "authored", "STOP": "authored", "NOTE": "authored", "CHECK": "performed"}


def sign(client, entry_type: str, title: str, **over):
    return sign_round(
        client,
        logbook="run_log",
        entry_type=entry_type,
        meaning=MEANING[entry_type],
        title=title,
        fields={},
        **over,
    )


@pytest.fixture
def runs(client, tmp_path):
    """The run log beside the demo log: one closed run with a check, then an open run."""
    registry.register(parse_logbook(RUN_LOG, source="test:w3-runs"))
    obligations.set_sender(lambda recipient, state: None)
    yield as_(client, OP1)
    obligations.reset_sender()
    intervals.reset_seeds()


def two_runs(c):
    """Run 1 opened, checked and closed; run 2 opened and left open."""
    recs = [
        sign(c, "START", "first run begins"),
        sign(c, "CHECK", "panel read"),
        sign(c, "STOP", "first run ends"),
        sign(c, "START", "second run begins"),
    ]
    return recs


# -- LB-W3-A1: the records route filters -----------------------------------------------------


@pytest.mark.acceptance("LB-W3-A1")
def test_lb_w3_a1_the_records_route_filters_and_the_filters_and_together(runs):
    c = runs
    two_runs(c)
    sign(c, "NOTE", "a loose note about the panel")
    base = "/api/v1/attest/run_log/records"

    def seqs(query: str) -> list[int]:
        r = c.get(f"{base}{query}")
        assert r.status_code == 200, r.text
        return [x["seq"] for x in r.json()["records"]]

    assert seqs("") == [1, 2, 3, 4, 5]
    assert seqs("?entry_type=CHECK") == [2]
    assert seqs("?entry_type=START&entry_type=STOP") == [1, 3, 4]
    assert seqs("?meaning=authored") == [1, 3, 4, 5]
    assert seqs("?signer=%40op1%3Asite-a") == [1, 2, 3, 4, 5]
    assert seqs("?signer=%40nobody%3Asite-a") == []
    assert seqs("?interval=run%3A1") == [1, 2, 3]
    assert seqs("?interval=run%3A2") == [4]
    assert seqs("?q=panel") == [2, 5]
    assert seqs("?q=PANEL") == [2, 5]
    # AND, not OR: the check in run 1 that mentions the panel.
    assert seqs("?q=panel&interval=run%3A1") == [2]
    assert seqs("?q=panel&entry_type=NOTE") == [5]
    assert seqs("?q=panel&entry_type=STOP") == []
    # Time bounds are ISO 8601 and inclusive; everything here was signed just now. A client
    # percent-encodes the bound (a bare "+" in a query is a space).
    earlier = quote((datetime.now(UTC) - timedelta(days=1)).isoformat())
    later = quote((datetime.now(UTC) + timedelta(days=1)).isoformat())
    assert seqs(f"?from={earlier}") == [1, 2, 3, 4, 5]
    assert seqs(f"?to={earlier}") == []
    assert seqs(f"?from={earlier}&to={later}&entry_type=CHECK") == [2]
    assert seqs(f"?from={later}") == []
    # An unmatched filter is an empty list, never an error.
    assert seqs("?entry_type=NOPE") == []
    assert seqs("?interval=run%3A9") == []
    assert seqs("?interval=shift%3A1") == []


@pytest.mark.acceptance("LB-W3-A1")
def test_lb_w3_a1_limit_clamps_and_filters_never_widen_the_site(runs):
    c = runs
    two_runs(c)
    base = "/api/v1/attest/run_log/records"
    assert [x["seq"] for x in c.get(f"{base}?limit=0").json()["records"]] == [4]
    assert [x["seq"] for x in c.get(f"{base}?limit=2").json()["records"]] == [3, 4]
    big = c.get(f"{base}?limit=9999").json()
    assert len(big["records"]) == 4
    # The page is at most PAGE_LIMIT rows, applied by the database, and says so.
    assert big["page"]["limit"] == 200
    # A filter is a narrowing of the site's own records and never a way to read another site's.
    r = c.get(f"{base}?site=site-b&entry_type=START")
    assert r.status_code == 403 and r.json()["code"] == "site"
    assert c.get(f"{base}?site=site-b&q=run").status_code == 403


@pytest.mark.acceptance("LB-W3-A1")
def test_lb_w3_a1_a_malformed_time_or_interval_filter_is_refused_with_a_reason(runs):
    c = runs
    two_runs(c)
    base = "/api/v1/attest/run_log/records"
    r = c.get(f"{base}?from=yesterday")
    assert r.status_code == 400 and "from" in r.json()["detail"]
    r = c.get(f"{base}?interval=run")
    assert r.status_code == 400 and "kind:number" in r.json()["detail"]


# -- LB-W3-A1 at scale: keyset pages, no count, a page's ETag, a verified slice ---------------


@pytest.mark.acceptance("LB-W3-A1")
def test_lb_w3_a1_pages_are_a_keyset_on_seq_in_both_directions_with_no_count(runs):
    c = runs
    two_runs(c)
    sign(c, "NOTE", "five")
    sign(c, "NOTE", "six")
    base = "/api/v1/attest/run_log/records"
    newest = c.get(f"{base}?limit=2").json()
    assert [x["seq"] for x in newest["records"]] == [5, 6]
    assert newest["page"] == {"limit": 2, "before_seq": None, "after_seq": None, "has_more": True,
                              "oldest_seq": 5, "newest_seq": 6}  # fmt: skip
    assert "count" not in newest and "total" not in newest
    # Older: the rows before a sequence, newest of them last; the page says when nothing older is left.
    older = c.get(f"{base}?limit=2&before_seq=5").json()
    assert [x["seq"] for x in older["records"]] == [3, 4] and older["page"]["has_more"] is True
    oldest = c.get(f"{base}?limit=2&before_seq=3").json()
    assert [x["seq"] for x in oldest["records"]] == [1, 2] and oldest["page"]["has_more"] is False
    assert c.get(f"{base}?limit=2&before_seq=1").json()["records"] == []
    # Newer: the rows after a sequence, the direction a live tail reads, oldest of them first.
    newer = c.get(f"{base}?limit=2&after_seq=2").json()
    assert [x["seq"] for x in newer["records"]] == [3, 4] and newer["page"]["has_more"] is True
    assert [x["seq"] for x in c.get(f"{base}?limit=10&after_seq=4").json()["records"]] == [5, 6]
    assert c.get(f"{base}?after_seq=6").json()["page"]["has_more"] is False
    # A filter pages the same way, inside its own index.
    notes = c.get(f"{base}?entry_type=NOTE&limit=1").json()
    assert [x["seq"] for x in notes["records"]] == [6] and notes["page"]["has_more"] is True
    assert [
        x["seq"] for x in c.get(f"{base}?entry_type=NOTE&limit=1&before_seq=6").json()["records"]
    ] == [5]


@pytest.mark.acceptance("LB-W3-A1")
def test_lb_w3_a1_an_unchanged_page_answers_304_to_its_etag(runs):
    c = runs
    two_runs(c)
    base = "/api/v1/attest/run_log/records"
    first = c.get(f"{base}?limit=2")
    etag = first.headers["etag"]
    assert etag.startswith('W/"')
    again = c.get(f"{base}?limit=2", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.headers["etag"] == etag
    # A new entry changes the newest page, so its ETag changes and the page is served again.
    sign(c, "NOTE", "five")
    changed = c.get(f"{base}?limit=2", headers={"If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["etag"] != etag
    # The older page it did not touch keeps its ETag.
    assert (
        c.get(f"{base}?limit=2&before_seq=3").headers["etag"]
        == c.get(f"{base}?limit=2&before_seq=3").headers["etag"]
    )


@pytest.mark.acceptance("LB-W3-A1")
def test_lb_w3_a1_a_page_verifies_its_own_slice_not_the_whole_chain(runs):
    c = runs
    two_runs(c)
    whole = c.post("/api/v1/attest/run_log/verify").json()
    assert whole["ok"] and whole["checked"] == 4 and whole["from_seq"] is None
    part = c.post("/api/v1/attest/run_log/verify?from_seq=3&to_seq=4").json()
    assert part["ok"] and part["checked"] == 2 and part["head_seq"] == 4
    assert (part["from_seq"], part["to_seq"]) == (3, 4)
    tail = c.post("/api/v1/attest/run_log/verify?from_seq=4").json()
    assert tail["ok"] and tail["checked"] == 1
    bad = c.post("/api/v1/attest/run_log/verify?from_seq=5&to_seq=2")
    assert bad.status_code == 400 and bad.json()["code"] == "slice"
    assert c.post("/api/v1/attest/run_log/verify?site=site-b&from_seq=1").status_code == 403


@pytest.mark.acceptance("LB-W3-A1")
def test_lb_w3_a1_the_text_filter_is_the_databases_search_projection(runs):
    """Words, each as a prefix, all required; answered from the generated search column."""
    from axiom.extensions.builtins.attest import service

    c = runs
    two_runs(c)
    sign(c, "NOTE", "the pumps were noted")
    base = "/api/v1/attest/run_log/records"
    assert [x["seq"] for x in c.get(f"{base}?q=pump").json()["records"]] == [5]
    assert [x["seq"] for x in c.get(f"{base}?q=pump%20noted").json()["records"]] == [5]
    assert [x["seq"] for x in c.get(f"{base}?q=begins").json()["records"]] == [1, 4]
    assert c.get(f"{base}?q=%26%26%26").json()["records"] == []
    assert service.text_query("pump noted") == "pump:* & noted:*"
    assert service.text_query("  ") is None


# -- LB-W3-A2: the brief carries the interval --------------------------------------------------


@pytest.mark.acceptance("LB-W3-A2")
def test_lb_w3_a2_a_record_brief_carries_its_interval_or_null(runs):
    c = runs
    two_runs(c)
    sign(c, "NOTE", "outside any run")
    by_seq = {x["seq"]: x for x in c.get("/api/v1/attest/run_log/records").json()["records"]}
    assert by_seq[1]["interval"] == {"kind": "run", "number": 1, "event": "opened"}
    assert by_seq[2]["interval"] == {"kind": "run", "number": 1}
    assert by_seq[3]["interval"] == {"kind": "run", "number": 1, "event": "closed"}
    assert by_seq[4]["interval"] == {"kind": "run", "number": 2, "event": "opened"}
    assert "interval" in by_seq[5] and by_seq[5]["interval"] is None
    # The demo log declares no intervals: every brief carries null, not a missing key.
    sign_round(c)
    demo = c.get("/api/v1/attest/demo_log/records").json()["records"]
    assert demo and all(x["interval"] is None for x in demo)


@pytest.mark.acceptance("LB-W3-A2")
def test_lb_w3_a2_the_live_stream_payload_carries_the_interval(runs):
    c = runs
    two_runs(c)
    with c.stream(
        "GET", "/api/v1/attest/run_log/stream?once=1", headers={"Last-Event-ID": "0"}
    ) as r:
        body = "".join(r.iter_text())
    payloads = [
        json.loads(line[len("data: ") :]) for line in body.splitlines() if line.startswith("data: ")
    ]
    signed = {p["seq"]: p for p in payloads if "seq" in p}
    assert signed[1]["interval"] == {"kind": "run", "number": 1, "event": "opened"}
    assert signed[2]["interval"] == {"kind": "run", "number": 1}
    assert signed[4]["interval"] == {"kind": "run", "number": 2, "event": "opened"}


# -- LB-W3-A3 and A4: the intervals route ------------------------------------------------------


@pytest.mark.acceptance("LB-W3-A3")
def test_lb_w3_a3_the_intervals_route_lists_each_interval_its_counts_and_the_gaps(runs, tmp_path):
    c = runs
    recs = two_runs(c)
    # A check falls due inside the open run and nobody signs it: one missed event in run 2.
    changes = obligations.tick(
        SITE, "run_log", now=datetime.now(UTC) + timedelta(hours=2), state_dir=tmp_path
    )
    assert [ch["state"] for ch in changes] == ["missed"]

    r = c.get("/api/v1/attest/run_log/intervals")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["site_id"] == SITE and body["logbook"] == "run_log"
    assert body["kinds"] == ["run"]
    runs_ = body["intervals"]
    assert [(i["kind"], i["number"], i["state"]) for i in runs_] == [
        ("run", 1, "closed"),
        ("run", 2, "open"),
    ]
    first, second = runs_
    assert first["opened_by"] == recs[0]["attestation_id"]
    assert first["closed_by"] == recs[2]["attestation_id"]
    assert first["opened_at"] and first["closed_at"]
    assert isinstance(first["duration_seconds"], (int, float)) and first["duration_seconds"] >= 0
    assert first["records"] == 3 and first["missed"] == 0  # the zero is a zero, never hidden
    assert second["opened_by"] == recs[3]["attestation_id"]
    assert second["closed_by"] is None and second["closed_at"] is None
    assert second["duration_seconds"] is None
    assert second["records"] == 1 and second["missed"] == 1
    # One gap: between run 1's close and run 2's open. None before the first or after the open one.
    assert len(body["gaps"]) == 1
    gap = body["gaps"][0]
    assert gap["kind"] == "run" and gap["after"] == 1 and gap["before"] == 2
    assert gap["from"] == first["closed_at"] and gap["to"] == second["opened_at"]
    assert isinstance(gap["seconds"], (int, float)) and gap["seconds"] >= 0
    # Narrowed to a kind the logbook declares, the same; to one it does not, nothing.
    assert c.get("/api/v1/attest/run_log/intervals?kind=run").json()["intervals"] == runs_
    assert c.get("/api/v1/attest/run_log/intervals?kind=shift").json()["intervals"] == []


@pytest.mark.acceptance("LB-W3-A4")
def test_lb_w3_a4_another_site_is_refused_and_no_intervals_is_an_empty_list(runs):
    c = runs
    two_runs(c)
    r = c.get("/api/v1/attest/run_log/intervals?site=site-b")
    assert r.status_code == 403 and r.json()["code"] == "site"
    assert c.get("/api/v1/attest/run_log/intervals", cookies={}).status_code in (200, 401)
    none = c.get("/api/v1/attest/demo_log/intervals").json()
    assert none["intervals"] == [] and none["kinds"] == [] and none["gaps"] == []
    # The logbook JSON says which intervals a log declares, so a page can offer the view.
    assert c.get("/api/v1/attest/logbooks/run_log").json()["intervals"] == [
        {"kind": "run", "opens": ["START"], "closes": ["STOP"]}
    ]
    assert c.get("/api/v1/attest/logbooks/demo_log").json()["intervals"] == []


# -- LB-W3-A5: search narrows to one logbook ----------------------------------------------------


@pytest.mark.acceptance("LB-W3-A5")
def test_lb_w3_a5_search_query_narrows_to_one_logbook_and_keeps_the_site_scope(runs):
    c = runs
    sign(c, "NOTE", "valve seat inspected")
    sign_round(c, title="valve seat inspected again")

    def hits(**extra):
        params = {"query": "valve seat", "kinds": ["logbook"], "_sites": [SITE], **extra}
        result = search_run(params, None)
        assert result.ok, result.errors
        return sorted((h["meta"]["logbook"], h["id"]) for h in result.value["results"])

    both = hits()
    assert {b for b, _ in both} == {"demo_log", "run_log"}
    only = hits(logbook="demo_log")
    assert only and {b for b, _ in only} == {"demo_log"}
    assert hits(logbook="run_log") and {b for b, _ in hits(logbook="run_log")} == {"run_log"}
    assert hits(logbook="no_such_log") == []
    # The site scope still bounds a narrowed search.
    assert hits(logbook="demo_log", _sites=["site-b"]) == []
    # The engine's own door, the same.
    narrowed = engine.search(
        "valve seat", engine.Scope(sites=frozenset({SITE}), logbook="run_log"), kinds=["logbook"]
    )
    assert {h["meta"]["logbook"] for h in narrowed["results"]} == {"run_log"}


# -- LB-W3-A6: surfaces ------------------------------------------------------------------------


@pytest.mark.acceptance("LB-W3-A6")
def test_lb_w3_a6_the_new_reads_are_reads_and_signing_stays_on_the_cli():
    by_name = {s.name: s for s in SPECS}
    iv = by_name["attest.intervals"]
    assert iv.surfaces == ("cli", "mcp", "agent_tool")
    assert iv.side_effects is False and iv.idempotent is True and iv.risk == "read"
    show = by_name["attest.show"]
    assert show.side_effects is False and show.surfaces == ("cli", "mcp", "agent_tool")
    for key in ("type", "signer", "since", "until", "interval", "text"):
        assert key in show.inputs, key
    assert by_name["attest.new"].surfaces == ("cli",)
    assert by_name["attest.sign"].surfaces == ("cli",)


@pytest.mark.acceptance("LB-W3-A6")
def test_lb_w3_a6_the_show_skill_takes_the_same_filters(runs):
    from axiom.extensions.builtins.attest.skills import read

    c = runs
    two_runs(c)
    sign(c, "NOTE", "a loose note about the panel")

    def seqs(**params):
        result = read.show({"site": SITE, "logbook": "run_log", **params})
        assert result.ok, result.errors
        return [r["seq"] for r in result.value["records"]]

    assert seqs() == [1, 2, 3, 4, 5]
    assert seqs(type="CHECK") == [2]
    assert seqs(type=["START", "STOP"]) == [1, 3, 4]
    assert seqs(signer="@op1:site-a", interval="run:1") == [1, 2, 3]
    assert seqs(text="panel") == [2, 5]
    assert seqs(since=(datetime.now(UTC) + timedelta(days=1)).isoformat()) == []
    assert seqs(until=(datetime.now(UTC) + timedelta(days=1)).isoformat()) == [1, 2, 3, 4, 5]
    assert seqs(type="NOPE") == []
    result = read.show({"site": SITE, "logbook": "run_log", "interval": "run"})
    assert not result.ok and "kind:number" in " ".join(result.errors)


@pytest.mark.acceptance("LB-W3-A3")
def test_lb_w3_a3_the_intervals_skill_answers_like_the_route(runs):
    from axiom.extensions.builtins.attest.skills import read

    c = runs
    two_runs(c)
    result = read.intervals_listing({"site": SITE, "logbook": "run_log"})
    assert result.ok, result.errors
    assert [(i["number"], i["state"]) for i in result.value["intervals"]] == [
        (1, "closed"),
        (2, "open"),
    ]
    assert len(result.value["gaps"]) == 1 and result.value["kinds"] == ["run"]
    # The skill's site rule is the node's (resolve_site: a site this node does not serve is
    # refused; the route adds the session's claim). Another served site has none of these runs.
    other = read.intervals_listing({"site": "site-b", "logbook": "run_log"})
    assert other.ok and other.value["intervals"] == [] and other.value["gaps"] == []
