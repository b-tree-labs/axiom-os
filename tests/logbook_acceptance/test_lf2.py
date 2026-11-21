# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for filtering a log by its own fields, phase 2 (PRD attestation 6c, rows
LF-A24 to LF-A27; ADR-268): ``datetime`` filters, the lowest and highest values a reader may see
for a number or quantity range, the node search taking the same filters as one query, and the
scale budget.

The log is ``logbooks/filter_log.toml`` (phase 1's, with a ``due`` time on its rounds). LF-A24
runs only in the scale job (``AXIOM_SCALE_TESTS=1``); ``AXIOM_LF_SCALE_ROWS`` runs it smaller.
"""

from __future__ import annotations

import os
import time

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import field_index, registry, store  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, OP2, as_  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_skills import _run, ctx  # noqa: E402

from .test_lf import (  # noqa: E402
    FILTER_LOG,
    LOG,
    OTHER,
    RECORDS,
    SITE,
    THEM,
    correct,
    entry,
    q,
    run_cli,
    seqs,
)


@pytest.fixture
def flog(client, node_signer, tmp_path, monkeypatch):
    """The filter log beside the demo log, OP1 signed in (phase 1's fixture)."""
    registry.register_file(FILTER_LOG)
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    return as_(client, OP1)

# -- LF-A25: a datetime filter is an inclusive range of instants -------------------------------


def _due_entries():
    entry({"due": "2026-11-01T08:00:00Z"}, title="first")  # 1
    entry({"due": "2026-11-02T08:00:00+02:00"}, title="second")  # 2: 06:00 UTC
    entry({"due": "2026-11-03T00:00:00"}, title="third")  # 3: a naive time is UTC
    entry({"due": "next tuesday"}, title="words")  # 4: not a time, indexed as text only
    entry({"area": "North"}, title="no due")  # 5


@pytest.mark.acceptance("LF-A25")
def test_lf_a25_a_datetime_filter_keeps_an_inclusive_range_of_instants(flog):
    _due_entries()
    assert seqs(flog, q(field__due__gte="2026-11-02T00:00:00Z")) == [2, 3]
    assert seqs(flog, q(field__due__lte="2026-11-02T06:00:00Z")) == [1, 2]
    # The same instant written with an offset is the same bound, inclusive at both ends.
    assert seqs(flog, q(field__due__gte="2026-11-02T08:00:00+02:00",
                       field__due__lte="2026-11-02T08:00:00+02:00")) == [2]  # fmt: skip
    # A naive bound is UTC, as the records door's from and to are.
    assert seqs(flog, q(field__due__gte="2026-11-03T00:00:00")) == [3]
    # ANDed with the platform's filters.
    assert seqs(flog, q(field__due__gte="2026-11-01T00:00:00Z", q="second")) == [2]


@pytest.mark.acceptance("LF-A25")
def test_lf_a25_a_time_signed_without_an_offset_is_the_sites_wall_time(flog, monkeypatch):
    # The entry form signs the person's wall time; at a site whose clock names a zone, that zone
    # places it (a site that names none is UTC, as above).
    from axiom.extensions.builtins.attest import periods

    monkeypatch.setenv(periods.SITE_TZ_ENV, "America/Chicago")
    entry({"due": "2026-11-03T00:00"})  # midnight in Chicago is 06:00 UTC
    entry({"due": "2026-11-03T00:00:00Z"})  # an offset is kept as written
    assert seqs(flog, q(field__due__gte="2026-11-03T05:59:00Z", field__due__lte="2026-11-03T06:00:00Z")) == [1]
    assert seqs(flog, q(field__due__lte="2026-11-03T00:00:00Z")) == [2]


@pytest.mark.acceptance("LF-A25")
def test_lf_a25_a_bad_datetime_bound_is_refused_with_the_same_words_everywhere(
    flog, tmp_path, monkeypatch, capsys
):
    entry({"due": "2026-11-01T08:00:00Z"})
    r = flog.get(f"{RECORDS}?site={SITE}&{q(field__due__gte='tomorrow')}")
    assert r.status_code == 400 and r.json()["code"] == "filter"
    words = r.json()["detail"]
    assert "field.due.gte" in words and "ISO 8601" in words
    s = _run("attest.show", {"site": SITE, "logbook": LOG, "field.due.gte": "tomorrow"}, ctx(tmp_path))
    assert not s.ok and s.errors == [words]
    code, _out, err = run_cli(["show", "--site", SITE, "--where", "due>=tomorrow"], tmp_path,
                              monkeypatch, capsys)  # fmt: skip
    assert code == 2 and words in err
    # A time takes no unit.
    r = flog.get(f"{RECORDS}?site={SITE}&{q(field__due__gte='2026-11-01T00:00:00Z', field__due__unit='h')}")
    assert r.status_code == 400 and "takes no unit" in r.json()["detail"]
    # And no equality or contains: it is a range.
    r = flog.get(f"{RECORDS}?site={SITE}&field.due=2026-11-01")
    assert r.status_code == 400 and "field.due.gte" in r.json()["detail"]


@pytest.mark.acceptance("LF-A25")
def test_lf_a25_every_surface_offers_the_datetime_filter_and_the_cli_takes_it(
    flog, tmp_path, monkeypatch, capsys
):
    from axiom.extensions.builtins.attest import completion_values

    book = registry.get(LOG)
    assert "filter.due" not in book.not_yet_enforced
    listed = {f["field"]: f for f in flog.get(f"/api/v1/attest/logbooks/{LOG}").json()["filters"]}
    assert listed["due"]["control"] == "range" and listed["due"]["field_type"] == "datetime"
    assert listed["due"]["unit"] is None and listed["due"]["units"] == []
    _due_entries()
    code, out, err = run_cli(["show", "--site", SITE, "--json", "--where", "due>=2026-11-02T00:00:00Z",
                              "--where", "due<=2026-11-02T23:59:59Z"], tmp_path, monkeypatch, capsys)  # fmt: skip
    assert code == 0, err
    import json

    assert [r["title"] for r in json.loads(out)["value"]["records"]] == ["second"]
    monkeypatch.setattr(completion_values, "_ctx", lambda: ctx(tmp_path)[0])
    monkeypatch.setattr(completion_values, "_sites", lambda _c: [SITE])
    got = completion_values.collect(None)["log_filter"]
    assert "due>=" in got and "due<=" in got


@pytest.mark.acceptance("LF-A25")
def test_lf_a25_a_correction_moves_the_time_and_doctor_counts_words(flog, tmp_path):
    from axiom.cli.ext.commands import doctor

    from .test_lf import _ext

    rec = entry({"due": "2026-11-01T08:00:00Z"})
    correct(rec, {"due": "2026-12-01T08:00:00Z"})
    assert seqs(flog, q(field__due__lte="2026-11-30T00:00:00Z")) == []
    assert seqs(flog, q(field__due__gte="2026-11-30T00:00:00Z")) == [1]
    entry({"due": "next tuesday"})
    seqs(flog, q(field__due__gte="2026-11-30T00:00:00Z"))
    registry.reset()
    found = doctor._check_logbook_filters(_ext(tmp_path))
    assert "1 value of due could only be indexed as text" in " | ".join(r.detail for r in found)


# -- LF-A26: the lowest and highest values a reader may see, for a range ----------------------


def _ranges(c, site=SITE):
    r = c.get(f"/api/v1/attest/{LOG}/filters?site={site}")
    assert r.status_code == 200, r.text
    return {f["field"]: f.get("range") for f in r.json()["filters"]}


@pytest.mark.acceptance("LF-A26")
def test_lf_a26_a_range_offers_the_lowest_and_highest_values_seen_in_its_unit(flog, tmp_path):
    assert _ranges(flog)["pressure"] is None  # nothing signed yet: nothing to offer
    entry({"pressure": "3.5", "count": "2"})
    entry({"pressure": {"value": "450", "unit": "kPa"}, "count": "9"})  # 4.5 bar
    entry({"pressure": "1.25"}, type_="SPOT")
    entry({"pressure": {"value": "60", "unit": "psi"}})  # no unit a filter compares
    got = _ranges(flog)
    assert got["pressure"] == {"lowest": "1.25", "highest": "4.5", "unit": "bar"}
    assert got["count"] == {"lowest": "2", "highest": "9", "unit": None}
    # Only ranges offer one; a pick list or a box does not.
    assert got["area"] is None and got["asset_id"] is None and got["checked"] is None
    # The skill (MCP) answers the same.
    r = _run("attest.filters", {"site": SITE, "logbook": LOG}, ctx(tmp_path))
    assert {f["field"]: f.get("range") for f in r.value["filters"]}["pressure"] == got["pressure"]


@pytest.mark.acceptance("LF-A26")
def test_lf_a26_the_range_comes_only_from_entries_the_reader_may_read(flog):
    entry({"pressure": "4", "count": "5"})
    big = entry({"count": "50"})
    correct(big, {"count": "6"})  # the current value counts, never the one corrected away
    entry({"pressure": "99", "count": "100"}, site=OTHER, who=THEM)  # another site's entries
    entry({"pressure": "0.5"}, type_="SPOT", site=OTHER, who=THEM)
    got = _ranges(flog)
    assert got["pressure"] == {"lowest": "4", "highest": "4", "unit": "bar"}
    assert got["count"] == {"lowest": "5", "highest": "6", "unit": None}
    # The other site's values are behind the one check: the same refusal as its records.
    refused = flog.get(f"/api/v1/attest/{LOG}/filters?site={OTHER}")
    plain = flog.get(f"{RECORDS}?site={OTHER}")
    assert refused.status_code == 403 and (refused.status_code, refused.json()) == (plain.status_code, plain.json())
    # A reader who may not read the log gets the records door's refusal, and no range.
    as_(flog, OP2)
    refused = flog.get(f"/api/v1/attest/{LOG}/filters?site={SITE}")
    plain = flog.get(f"{RECORDS}?site={SITE}")
    assert (refused.status_code, refused.json()) == (plain.status_code, plain.json())
    assert "range" not in refused.text
    as_(flog, OP1)


# -- LF-A24: the scale budget --------------------------------------------------------------------

LF_ROWS = 100_000


@pytest.mark.scale
@pytest.mark.acceptance("LF-A24")
def test_lf_a24_a_field_filtered_first_page_of_100k_entries_is_within_the_page_budget(client):
    from axiom.extensions.builtins.attest import scale_fixture
    from axiom.extensions.builtins.attest.logbooks import parse_logbook
    from axiom.extensions.builtins.attest.tests.test_signing import SITE as FIX_SITE

    from . import ls1_budgets

    if os.environ.get("AXIOM_SCALE_TESTS") != "1":
        pytest.skip("scale budgets run in the nightly scale job (AXIOM_SCALE_TESTS=1)")
    rows = int(os.environ.get("AXIOM_LF_SCALE_ROWS", str(LF_ROWS)))
    log = "scale_log_fields"
    registry.register(parse_logbook(scale_fixture.declaration(log), source="lf-a24"))
    with store.session_scope() as s:
        scale_fixture.seed_records(s, site=FIX_SITE, n=rows, logbooks=[log])
    t0 = time.perf_counter()
    projected = field_index.catch_up(FIX_SITE, log)
    built = time.perf_counter() - t0
    assert projected == rows
    print(f"LF-A24 field index of {rows} entries built in {built:.1f} s")
    with store.session_scope() as s:
        s.execute(field_index.analyze_statement())
        s.commit()
    c = as_(client, OP1)
    page = f"/api/v1/attest/{log}/records?site={FIX_SITE}&limit=200&field.area=North"

    def ok(r):
        assert r.status_code == 200, r.text
        got = r.json()["records"]
        assert len(got) == 200 and all(x["fields"]["area"] == "North" for x in got)

    numbers = {"field_filter_first_page": ls1_budgets.p95_ms(lambda: c.get(page), check=ok)}
    ls1_budgets.record(numbers, rows=rows)
    over = ls1_budgets.over(numbers)
    assert not over, f"p95 at or over budget at {rows} entries: {over}"
