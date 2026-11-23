# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for filtering a log by its own fields, phase 1 (PRD attestation 6c, rows
LF-A1 to LF-A22; ADR-268). LF-A19 to LF-A21 are the Log page's and live in appkit; LF-A23 rides
the door registry of #1758 (LD-1).

The log is ``logbooks/filter_log.toml``: rounds and spot checks with a choice, an exact-value
text, a quantity in bar that accepts kPa, a checkbox, a note, a count and one observed value.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest
from sqlalchemy import text

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import (  # noqa: E402
    field_index,
    log_cli,
    registry,
    service,
    signing,
    store,
)
from axiom.extensions.builtins.attest.logbooks import LogbookError, parse_logbook  # noqa: E402
from axiom.extensions.builtins.attest.skills import SPECS  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, OP2, as_  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import person  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_skills import _run, ctx  # noqa: E402

HERE = Path(__file__).parent
FILTER_LOG = HERE / "logbooks" / "filter_log.toml"
LOG = "filter_log"
SITE = "site-a"
OTHER = "site-b"
ME = person(roles=("operator",), handle="@op1:site-a")
THEM = person(roles=("operator",), handle="@op9:site-b")
RECORDS = f"/api/v1/attest/{LOG}/records"


@pytest.fixture
def flog(client, node_signer, tmp_path, monkeypatch):
    """The filter log beside the demo log, OP1 signed in; signing through the service."""
    registry.register_file(FILTER_LOG)
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    return as_(client, OP1)


def entry(fields, *, type_="ROUND", site=SITE, who=ME, title=None, occurred_at=None):
    et = registry.get(LOG).type(type_)
    d = service.create_draft(
        site_id=site, logbook=LOG, entry_type=type_, meaning=et.meanings[0],
        content={"title": title or type_.title(), "fields": fields}, origin="human",
        for_principal=who.principal.handle,
        **({"occurred_at": occurred_at} if occurred_at else {}),
    )  # fmt: skip
    pres = service.present(d)
    service.respond(pres.presentation_id, principal=who.principal, answer="sign", via="cli")
    return service.sign(pres.presentation_id, signatory=who, signer=signing.signer()).record


def correct(rec, fields, who=ME):
    d = service.create_supplement_draft(site_id=rec["site_id"], target=rec["attestation_id"],
                                        reason="correction", content={"fields": fields},
                                        origin="human", for_principal=who.principal.handle)  # fmt: skip
    pres = service.present(d)
    service.respond(pres.presentation_id, principal=who.principal, answer="sign", via="cli")
    return service.sign(pres.presentation_id, signatory=who, signer=signing.signer()).record


def seqs(c, query: str = "", site: str = SITE) -> list[int]:
    r = c.get(f"{RECORDS}?site={site}&{query}")
    assert r.status_code == 200, r.text
    return [x["seq"] for x in r.json()["records"]]


def q(**pairs) -> str:
    out = []
    for k, v in pairs.items():
        for one in v if isinstance(v, list) else [v]:
            out.append(f"{k.replace('__', '.')}={quote(str(one))}")
    return "&".join(out)


def run_cli(argv, roles_dir, monkeypatch, capsys):
    made = ctx(roles_dir)[0]
    monkeypatch.setattr(log_cli, "_build_ctx", lambda: made)
    code = log_cli.main(["filters", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def access_records(state_dir: Path) -> list[dict]:
    from axiom.infra import audit_trail

    return [r for r in audit_trail.read_chain(audit_trail.audit_path(state_dir)) if r.get("access")]


def chain_ok(site=SITE) -> bool:
    return service.verify_logbook(site, LOG, signing.public_keys()).ok


# -- LF-A1: the declaration loads and every surface lists it ---------------------------------


@pytest.mark.acceptance("LF-A1")
def test_lf_a1_a_log_with_filters_loads_and_every_surface_lists_them(flog, tmp_path, capsys):
    book = registry.get(LOG)
    assert [f.field for f in book.field_filters] == [
        "area", "asset_id", "pressure", "note", "checked", "count", "seen", "due"]  # fmt: skip
    # GET /logbooks/{logbook}
    listed = {f["field"]: f for f in flog.get(f"/api/v1/attest/logbooks/{LOG}").json()["filters"]}
    assert listed["area"]["control"] == "choice" and listed["area"]["label"] == "Area"
    assert listed["asset_id"]["control"] == "values"
    assert listed["note"]["control"] == "words" and listed["note"]["label"] == "Note text"
    assert listed["pressure"]["control"] == "range" and listed["pressure"]["units"] == ["bar", "kPa"]
    assert listed["checked"]["control"] == "checkbox"
    assert listed["count"]["control"] == "range"
    # axiom_attest__filters
    r = _run("attest.filters", {"site": SITE, "logbook": LOG}, ctx(tmp_path))
    assert r.ok, r.errors
    got = {f["field"]: f for f in r.value["filters"]}
    assert got["pressure"]["unit"] == "bar" and got["pressure"]["units"] == ["bar", "kPa"]
    assert got["note"]["types"] == ["ROUND"] and got["area"]["types"] == ["ROUND", "SPOT"]
    # `log <log> about`
    about = _run("attest.logbook_list", {"logbook": LOG}, ctx(tmp_path))
    assert [f["field"] for f in about.value["logbooks"][0]["filters"]][:3] == ["area", "asset_id", "pressure"]
    log_cli._print_text(about)
    assert "Pressure (bar, kPa)" in capsys.readouterr().out


# -- LF-A2: validation refuses, naming the filter and the fix ---------------------------------

BASE_DECL = {
    "logbook": {"id": "v_log", "version": "1"},
    "type": [
        {"id": "A", "meanings": ["authored"], "roles": ["r"], "fields": [
            {"id": "x", "type": "text"}, {"id": "q", "type": "quantity", "unit": "bar"},
            {"id": "c", "type": "choice", "choices": ["a", "b"]},
            {"id": "o", "type": "obligation_ref"}, {"id": "when", "type": "datetime"},
            {"id": "r", "type": "readings"}]},
        {"id": "B", "meanings": ["authored"], "roles": ["r"], "fields": [
            {"id": "x", "type": "number"}, {"id": "q", "type": "quantity", "unit": "kPa"},
            {"id": "c", "type": "choice", "choices": ["a", "z"]}]},
        {"id": "C", "meanings": ["authored"], "roles": ["r"], "fields": [{"id": "y", "type": "text"}]},
    ],
}  # fmt: skip


def _value(v) -> str:
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {_value(x)}" for k, x in v.items()) + " }"
    if isinstance(v, list):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    return json.dumps(v)


def _toml(data: dict) -> str:
    out = []
    for key, table in data.items():
        for t in table if isinstance(table, list) else [table]:
            out.append(f"[[{key}]]" if isinstance(table, list) else f"[{key}]")
            out += [f"{k} = {_value(v)}" for k, v in t.items()]
    return "\n".join(out) + "\n"


@pytest.mark.acceptance("LF-A2")
@pytest.mark.parametrize("filters, words", [
    ([{"field": "nope"}], "no entry type declares a field 'nope'"),
    ([{"field": "y", "types": ["D"]}], "types names 'D', which is not an entry type"),
    ([{"field": "y", "types": ["A"]}], "entry type 'A' declares no field 'y'"),
    ([{"field": "o", "types": ["A"]}], "obligation_ref field, which cannot be a filter"),
    ([{"field": "x"}], "A and B declare 'x' differently"),
    ([{"field": "q"}], "A and B declare 'q' differently"),
    ([{"field": "c"}], "declare different choices for 'c'"),
    ([{"field": "q", "types": ["A"], "match": "equals"}], "`match` applies to a text field"),
    ([{"field": "y"}, {"field": "y"}], "another filter already covers 'y' on C"),
    ([{"field": "y", "colour": "red"}], "unknown key ['colour']"),
])  # fmt: skip
def test_lf_a2_validate_refuses_each_bad_filter_with_the_fix(filters, words, tmp_path):
    with pytest.raises(LogbookError) as e:
        parse_logbook({**BASE_DECL, "filter": filters})
    assert words in str(e.value)
    assert "[[filter]]" in str(e.value)
    # `log validate` refuses with the same words.
    path = tmp_path / "v.toml"
    path.write_text(_toml({**BASE_DECL, "filter": filters}), encoding="utf-8")
    r = _run("attest.logbook_validate", {"path": str(path)}, ctx(tmp_path))
    assert not r.ok and words in r.errors[0]


@pytest.mark.acceptance("LF-A2")
def test_lf_a2_a_readings_filter_loads_and_is_not_yet_enforced():
    # A datetime filter is in force from phase 2 (LF-A25); readings wait for phase 3.
    book = parse_logbook({**BASE_DECL, "filter": [{"field": "r"}, {"field": "when"}, {"field": "y"}]})
    assert "filter.r" in book.not_yet_enforced and "filter.when" not in book.not_yet_enforced
    assert [f.field for f in book.field_filters] == ["when", "y"]


# -- LF-A3: an external extension's log gains its filters the same way ------------------------


@pytest.mark.acceptance("LF-A3")
def test_lf_a3_an_external_extension_gains_the_same_filters(flog, tmp_path):
    builtin = _run("attest.filters", {"site": SITE, "logbook": LOG}, ctx(tmp_path)).value
    root = tmp_path / "site-packages-ext"
    ext = root / "outside_ext"
    ext.mkdir(parents=True)
    shutil.copy(FILTER_LOG, ext / "filter_log.toml")
    (ext / "axiom-extension.toml").write_text(
        '[extension]\nname = "outside_ext"\nversion = "0.0.1"\n\n'
        '[[extension.provides]]\nkind = "logbook"\nfile = "filter_log.toml"\n'
    )
    registry.reset()
    registry.discover(roots=[root])
    assert registry.get(LOG).source.startswith(str(root))
    external = _run("attest.filters", {"site": SITE, "logbook": LOG}, ctx(tmp_path)).value
    assert external == builtin


# -- LF-A4: equality, one value or any of several --------------------------------------------


@pytest.mark.acceptance("LF-A4")
def test_lf_a4_a_choice_filter_keeps_the_current_value_and_two_values_widen(flog):
    entry({"area": "North"})
    entry({"area": "South"})
    entry({"area": "East"})
    entry({"area": "North"}, type_="SPOT")
    assert seqs(flog, q(field__area="North")) == [1, 4]
    assert seqs(flog, q(field__area=["North", "East"])) == [1, 3, 4]
    assert seqs(flog, q(field__area="West")) == []


# -- LF-A5: a quantity compares exactly, in either accepted unit ------------------------------


@pytest.mark.acceptance("LF-A5")
def test_lf_a5_bar_and_kpa_bounds_give_the_same_entries_as_exact_decimals(flog):
    entry({"pressure": "3.9999999999999999999"})  # a float would call this 4
    entry({"pressure": "4"})
    entry({"pressure": {"value": "4.5", "unit": "bar"}})
    entry({"pressure": "3.5"}, type_="SPOT")
    in_bar = seqs(flog, q(field__pressure__gte="4", field__pressure__unit="bar"))
    in_kpa = seqs(flog, q(field__pressure__gte="400", field__pressure__unit="kPa"))
    assert in_bar == in_kpa == [2, 3]
    assert seqs(flog, q(field__pressure__gte="350", field__pressure__lte="400",
                       field__pressure__unit="kPa")) == [1, 2, 4]  # fmt: skip
    assert seqs(flog, q(field__count__lte="3")) == []


# -- LF-A6: a unit the field does not accept is refused everywhere -----------------------------


@pytest.mark.acceptance("LF-A6")
def test_lf_a6_a_bound_in_an_unaccepted_unit_is_refused_with_the_same_words(
    flog, tmp_path, monkeypatch, capsys
):
    entry({"pressure": "4"})
    r = flog.get(f"{RECORDS}?site={SITE}&{q(field__pressure__gte='4', field__pressure__unit='psi')}")
    assert r.status_code == 400 and r.json()["code"] == "filter"
    words = r.json()["detail"]
    assert "psi" in words and "bar" in words and "kPa" in words
    s = _run("attest.show", {"site": SITE, "logbook": LOG, "field.pressure.gte": "4",
                             "field.pressure.unit": "psi"}, ctx(tmp_path))  # fmt: skip
    assert not s.ok and s.errors == [words]
    code, _out, err = run_cli(["show", "--site", SITE, "--where", "pressure>=4 psi"], tmp_path,
                              monkeypatch, capsys)  # fmt: skip
    assert code == 2 and words in err


# -- LF-A7: only declared filters --------------------------------------------------------------


@pytest.mark.acceptance("LF-A7")
@pytest.mark.parametrize("name", ["hidden", "nope"])
def test_lf_a7_a_field_that_is_not_a_filter_is_refused_naming_the_filters(
    flog, tmp_path, monkeypatch, capsys, name
):
    r = flog.get(f"{RECORDS}?site={SITE}&field.{name}=x")
    assert r.status_code == 400 and r.json()["code"] == "filter"
    words = r.json()["detail"]
    assert words == (f"`{name}` is not a filter of this log; its filters are area, asset_id, "
                     "pressure, note, checked, count, seen, due")  # fmt: skip
    code, _out, err = run_cli(["show", "--site", SITE, "--where", f"{name}=x"], tmp_path,
                              monkeypatch, capsys)  # fmt: skip
    assert code == 2 and words in err


# -- LF-A8: field filters AND with everything, and page without gaps --------------------------


@pytest.mark.acceptance("LF-A8")
def test_lf_a8_field_filters_and_with_every_filter_and_with_each_other(flog):
    t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=5)
    entry({"area": "North", "asset_id": "A-1", "note": "loose cover"}, occurred_at=t0)          # 1
    entry({"area": "North", "asset_id": "A-2", "note": "fine"}, occurred_at=t0 + timedelta(hours=1))  # 2
    entry({"area": "North", "note": "loose cover"}, type_="SPOT", occurred_at=t0 + timedelta(hours=2))  # 3
    entry({"area": "South", "asset_id": "A-1"}, who=person(handle="@op2:site-a"),
          occurred_at=t0 + timedelta(hours=3))  # 4  # fmt: skip
    north = q(field__area="North")
    assert seqs(flog, north) == [1, 2, 3]
    assert seqs(flog, f"{north}&entry_type=SPOT") == [3]
    assert seqs(flog, f"{north}&meaning=performed") == [1, 2]
    assert seqs(flog, f"{q(field__asset_id='A-1')}&signer={quote('@op2:site-a')}") == [4]
    since = quote((t0 + timedelta(minutes=30)).isoformat())
    until = quote((t0 + timedelta(minutes=150)).isoformat())
    assert seqs(flog, f"{north}&from={since}&to={until}") == [2, 3]
    assert seqs(flog, f"{north}&q=cover") == [1, 3]
    assert seqs(flog, f"{north}&{q(field__asset_id='A-1')}") == [1]
    assert seqs(flog, f"{north}&{q(field__note__contains='loose')}") == [1]  # note covers ROUND only


@pytest.mark.acceptance("LF-A8")
def test_lf_a8_keyset_paging_pages_the_filtered_set_without_gaps_or_repeats(flog):
    for i in range(9):
        entry({"area": "North" if i % 3 else "South"})
    want = seqs(flog, q(field__area="North"))
    assert want == [2, 3, 5, 6, 8, 9]
    pages, before = [], None
    while True:
        extra = f"&before_seq={before}" if before else ""
        r = flog.get(f"{RECORDS}?site={SITE}&limit=2&{q(field__area='North')}{extra}").json()
        pages = [x["seq"] for x in r["records"]] + pages
        if not r["page"]["has_more"]:
            break
        before = r["page"]["oldest_seq"]
    assert pages == want
    forward = flog.get(f"{RECORDS}?site={SITE}&limit=2&after_seq=3&{q(field__area='North')}").json()
    assert [x["seq"] for x in forward["records"]] == [5, 6]
    seen, cursor = [], None
    while True:
        extra = f"&before={quote(cursor)}" if cursor else ""
        r = flog.get(f"{RECORDS}?site={SITE}&order=occurred&limit=4&{q(field__area='North')}{extra}").json()
        seen = [x["seq"] for x in r["records"]] + seen
        if not r["page"]["has_more"]:
            break
        cursor = r["page"]["oldest_key"]
    assert sorted(seen) == want and len(seen) == len(set(seen))


# -- LF-A9: the read check first; one refusal, recorded once ----------------------------------


@pytest.mark.acceptance("LF-A9")
def test_lf_a9_a_reader_who_may_not_read_is_refused_the_same_with_field_filters(flog, tmp_path):
    from axiom.extensions.builtins.attest.tests.test_api import User

    entry({"area": "North"})
    as_(flog, OP2)  # no role at site-a: the log does not exist for them
    plain = flog.get(f"{RECORDS}?site={SITE}")
    filtered = flog.get(f"{RECORDS}?site={SITE}&field.area=North")
    assert (plain.status_code, plain.json()) == (filtered.status_code, filtered.json())
    assert plain.status_code == 404
    away = User(user_id="op9", email="op9@example.org", name="Op Nine", roles=("user",),
                site=OTHER, attributes={"idp": "entra"})  # fmt: skip
    as_(flog, away)
    a = flog.get(f"{RECORDS}?site={SITE}")
    b = flog.get(f"{RECORDS}?site={SITE}&field.area=North")
    assert (a.status_code, a.json()) == (b.status_code, b.json()) and a.status_code == 403
    denied = [r for r in access_records(tmp_path) if r.get("outcome") == "denied"]
    assert len(denied) == 4  # one record per refused read, field filters or not
    # A field filter cannot name a site: there is no such filter.
    as_(flog, OP1)
    r = flog.get(f"{RECORDS}?site={SITE}&field.site_id={OTHER}")
    assert r.status_code == 400 and r.json()["code"] == "filter"


# -- LF-A10: corrections count ---------------------------------------------------------------


@pytest.mark.acceptance("LF-A10")
def test_lf_a10_a_correction_moves_the_entry_to_its_new_value(flog):
    rec = entry({"area": "North", "pressure": "4"})
    entry({"area": "South"})
    assert seqs(flog, q(field__area="North")) == [1]
    fix = correct(rec, {"area": "South", "pressure": "450", })
    assert fix["seq"] == 3
    assert seqs(flog, q(field__area="North")) == []
    assert seqs(flog, q(field__area="South")) == [1, 2]
    assert seqs(flog, q(field__pressure__gte="100")) == [1]
    again = correct(fix, {"area": "East"})  # a correction of the correction
    assert again["seq"] == 4
    assert seqs(flog, q(field__area="East")) == [1]
    assert seqs(flog, q(field__area="South")) == [2]


# -- LF-A11: read your writes, with no background job -----------------------------------------


@pytest.mark.acceptance("LF-A11")
def test_lf_a11_an_entry_signed_a_moment_ago_is_found_at_once(flog, monkeypatch):
    monkeypatch.setattr(field_index, "after_append", lambda *a, **k: None)  # no job runs
    entry({"asset_id": "A-104"})
    assert seqs(flog, q(field__asset_id="A-104")) == [1]
    entry({"asset_id": "A-104"})
    assert seqs(flog, q(field__asset_id="A-104")) == [1, 2]


# -- LF-A12: the index rebuilds to the same answers; no signed row changes --------------------


@pytest.mark.acceptance("LF-A12")
def test_lf_a12_reindex_gives_identical_results_and_touches_no_signed_row(
    flog, tmp_path, monkeypatch, capsys
):
    first = entry({"area": "North", "pressure": "4", "asset_id": "A-1", "checked": True})
    entry({"area": "South", "pressure": {"value": "390", "unit": "kPa"}, "note": "loose cover",
           "count": "7"})
    correct(first, {"area": "East"})
    queries = [q(field__area="East"), q(field__pressure__gte="3.95"), q(field__checked="true"),
               q(field__note__contains="loose"), q(field__count__gte="7"), q(field__asset_id="A-1")]  # fmt: skip
    before = [seqs(flog, x) for x in queries]
    with store.session_scope() as s:
        signed = s.execute(text("SELECT attestation_id, record FROM attest_records ORDER BY seq")).all()
    assert chain_ok()
    with store.session_scope() as s:
        s.execute(text("DELETE FROM attest_field_values"))
        s.execute(text("DELETE FROM attest_field_watermarks"))
        s.commit()
    code, out, err = run_cli(["reindex", "--site", SITE], tmp_path, monkeypatch, capsys)
    assert code == 0, err
    assert "reindexed" in out
    assert [seqs(flog, x) for x in queries] == before
    assert before == [[1], [1], [1], [2], [2], [1]]
    with store.session_scope() as s:
        after = s.execute(text("SELECT attestation_id, record FROM attest_records ORDER BY seq")).all()
    assert after == signed and chain_ok()


# -- LF-A13: a new filter finds old entries at once --------------------------------------------


@pytest.mark.acceptance("LF-A13")
def test_lf_a13_a_filter_declared_later_finds_the_old_entries_with_no_backfill(flog):
    import tomllib

    data = tomllib.loads(FILTER_LOG.read_text())
    data["filter"] = [f for f in data["filter"] if f["field"] != "asset_id"]
    registry.reset()
    registry.register(parse_logbook(data, source="test:lf-a13-v1"))
    entry({"asset_id": "A-7"})
    entry({"asset_id": "A-8"})
    assert flog.get(f"{RECORDS}?site={SITE}&field.asset_id=A-7").status_code == 400
    assert seqs(flog, q(field__area="North")) == []  # the index is at the head now
    registry.reset()
    registry.register(parse_logbook({**data, "filter": [*data["filter"], {"field": "asset_id",
                                     "match": "equals"}]}, source="test:lf-a13-v2"))  # fmt: skip
    assert seqs(flog, q(field__asset_id="A-7")) == [1]


# -- LF-A14: suggestions ----------------------------------------------------------------------


@pytest.mark.acceptance("LF-A14")
def test_lf_a14_a_choice_offers_its_choices_and_old_values_marked_as_no_longer_offered(flog, tmp_path):
    entry({"area": "West"})  # signed under an older declaration that offered West
    entry({"area": "North"})
    r = flog.get(f"/api/v1/attest/{LOG}/filters?site={SITE}")
    assert r.status_code == 200, r.text
    area = next(f for f in r.json()["filters"] if f["field"] == "area")
    assert area["values"] == [
        {"value": "North", "offered": True}, {"value": "South", "offered": True},
        {"value": "East", "offered": True}, {"value": "West", "offered": False},
    ]  # fmt: skip
    assert all("count" not in v for v in area["values"])
    skill = _run("attest.filters", {"site": SITE, "logbook": LOG}, ctx(tmp_path)).value
    assert next(f for f in skill["filters"] if f["field"] == "area")["values"] == area["values"]


@pytest.mark.acceptance("LF-A14")
def test_lf_a14_an_equals_filter_offers_at_most_fifty_values_and_a_prefix_lookup(flog):
    for i in range(55):
        entry({"asset_id": f"A-{i:03d}"})
    entry({"asset_id": "A-007"})
    r = flog.get(f"/api/v1/attest/{LOG}/filters?site={SITE}").json()
    asset = next(f for f in r["filters"] if f["field"] == "asset_id")
    assert len(asset["values"]) == 50
    assert asset["values"][0] == {"value": "A-007", "offered": True}  # the most frequent first
    looked = flog.get(f"/api/v1/attest/{LOG}/filters?site={SITE}&field=asset_id&prefix=A-05").json()
    only = looked["filters"]
    assert [f["field"] for f in only] == ["asset_id"]
    assert [v["value"] for v in only[0]["values"]] == [f"A-05{i}" for i in range(5)]
    words = next(f for f in r["filters"] if f["field"] == "note")
    assert words.get("values") is None  # a contains filter offers no values


# -- LF-A15: suggestions stay inside what the reader may read ---------------------------------


@pytest.mark.acceptance("LF-A15")
def test_lf_a15_no_suggestion_comes_from_another_site_and_the_read_is_recorded(flog, tmp_path):
    entry({"asset_id": "A-HERE"})
    entry({"asset_id": "B-ONLY"}, site=OTHER, who=THEM)
    r = flog.get(f"/api/v1/attest/{LOG}/filters?site={SITE}").json()
    asset = next(f for f in r["filters"] if f["field"] == "asset_id")
    assert [v["value"] for v in asset["values"]] == ["A-HERE"]
    assert flog.get(f"/api/v1/attest/{LOG}/filters?site={OTHER}").status_code == 403
    reads = [x for x in access_records(tmp_path) if x.get("skill") == "attest.read.filters"]
    assert reads and reads[0]["access"]["what"] == "filters"
    as_(flog, OP2)
    refused = flog.get(f"/api/v1/attest/{LOG}/filters?site={SITE}")
    plain = flog.get(f"{RECORDS}?site={SITE}")
    assert (refused.status_code, refused.json()) == (plain.status_code, plain.json())


# -- LF-A16: observed fields may be filters; nothing flows back into an entry ------------------


@pytest.mark.acceptance("LF-A16")
def test_lf_a16_an_observed_field_is_a_filter_and_no_entry_path_reads_the_index(flog):
    assert registry.get(LOG).type("ROUND").field("seen").observe
    entry({"seen": "gauge steady"})
    assert seqs(flog, q(field__seen="gauge steady")) == [1]
    # Software proposes an entry: the observed value is the person's to enter, never a suggestion.
    d = service.create_draft(site_id=SITE, logbook=LOG, entry_type="ROUND", meaning="performed",
                             content={"title": "Round", "fields": {"area": "North"}},
                             origin="agent:helper", for_principal=ME.principal.handle)  # fmt: skip
    draft = service.draft(d)
    assert "seen" not in draft["content"]["fields"]
    pres = service.present(d)
    assert "gauge steady" not in service.presentation_text(pres.presentation_id)
    # No entry path, proposal path or presenter reads the field index or the suggestions.
    root = Path(service.__file__).parent
    for name in ("service.py", "proposals.py", "presenters.py", "skills/propose.py", "skills/sign.py",
                 "changes.py"):  # fmt: skip
        source = (root / name).read_text(encoding="utf-8")
        assert "attest_field_values" not in source and "suggestions(" not in source, name
        assert "field_index.suggest" not in source, name


# -- LF-A17: every door gives the same records ------------------------------------------------


@pytest.mark.acceptance("LF-A17")
def test_lf_a17_the_skill_the_route_and_the_exports_agree(flog, tmp_path):
    entry({"area": "North", "pressure": "4.2"})
    entry({"area": "North", "pressure": "3"})
    entry({"area": "South", "pressure": "5"})
    entry({"area": "North", "pressure": "500", "note": "x"}, type_="SPOT")
    route = seqs(flog, q(field__area="North", field__pressure__gte="400", field__pressure__unit="kPa"))
    assert route == [1, 4]
    keys = {"field.area": "North", "field.pressure.gte": "400", "field.pressure.unit": "kPa"}
    where = [{"field": "area", "op": "eq", "value": "North"},
             {"field": "pressure", "op": "gte", "value": "400", "unit": "kPa"}]  # fmt: skip
    c = ctx(tmp_path)
    by_keys = _run("attest.show", {"site": SITE, "logbook": LOG, **keys}, c)
    by_where = _run("attest.show", {"site": SITE, "logbook": LOG, "where": where}, c)
    assert by_keys.ok and by_where.ok, (by_keys.errors, by_where.errors)
    assert sorted(r["seq"] for r in by_keys.value["records"]) == route
    assert sorted(r["seq"] for r in by_where.value["records"]) == route
    preview = flog.post(f"/api/v1/attest/{LOG}/exports/preview?site={SITE}", json=keys)
    assert preview.status_code == 200, preview.text
    assert preview.json()["selected"] == 2
    made = _run("attest.export", {"site": SITE, "logbook": LOG, "format": ["jsonl"], "where": where,
                                  "out": str(tmp_path / "pkg")}, c)  # fmt: skip
    assert made.ok, made.errors
    assert made.value["counts"]["selected"] == 2
    bad = flog.post(f"/api/v1/attest/{LOG}/exports/preview?site={SITE}", json={"field.hidden": "x"})
    assert bad.status_code == 400 and "is not a filter of this log" in bad.json()["detail"]


# -- LF-A18: the command line ------------------------------------------------------------------


@pytest.mark.acceptance("LF-A18")
def test_lf_a18_where_takes_each_operator_and_a_unit_and_repeats(flog, tmp_path, monkeypatch, capsys):
    entry({"area": "North", "pressure": "4", "note": "loose cover", "checked": True}, title="one")
    entry({"area": "North", "pressure": "5", "note": "fine", "checked": False}, title="two")
    entry({"area": "South", "pressure": "4.6"}, title="three")

    def titles(*where):
        argv = ["show", "--site", SITE, "--json"]
        for w in where:
            argv += ["--where", w]
        code, out, err = run_cli(argv, tmp_path, monkeypatch, capsys)
        assert code == 0, err
        return sorted(r["title"] for r in json.loads(out)["value"]["records"])

    assert titles("area=North") == ["one", "two"]
    assert titles("area=North", "pressure>=4.5 bar") == ["two"]
    assert titles("pressure<=450 kPa") == ["one"]
    assert titles("pressure >= 4.5") == ["three", "two"]
    assert titles("note~loose cover") == ["one"]
    assert titles("checked=yes") == ["one"]
    assert titles("checked=no") == ["two"]


@pytest.mark.acceptance("LF-A18")
def test_lf_a18_tab_completes_filter_names_and_offered_values(flog, tmp_path, monkeypatch):
    from axiom.extensions.builtins.attest import completion_values

    entry({"asset_id": "A-104"})
    show = next(s for s in SPECS if s.name == "attest.show")
    assert show.values.get("where") == "log_filter"
    monkeypatch.setattr(completion_values, "_ctx", lambda: ctx(tmp_path)[0])
    monkeypatch.setattr(completion_values, "_sites", lambda _c: [SITE])
    got = completion_values.collect(None)["log_filter"]
    for want in ("area=", "area=North", "asset_id=A-104", "pressure>=", "pressure<=", "note~",
                 "checked=yes", "checked=no", "count>="):  # fmt: skip
        assert want in got, want
    assert not any(v.startswith("hidden") for v in got)


# -- LF-A22: doctor -----------------------------------------------------------------------------


def _ext(tmp_path) -> Path:
    ext = tmp_path / "ext" / "filter_ext"
    ext.mkdir(parents=True)
    shutil.copy(FILTER_LOG, ext / "filter_log.toml")
    (ext / "axiom-extension.toml").write_text(
        '[extension]\nname = "filter_ext"\nversion = "0.0.1"\n\n[[extension.provides]]\n'
        'kind = "logbook"\nid = "filter_log"\nfile = "filter_log.toml"\n'
    )
    return ext


@pytest.mark.acceptance("LF-A22")
def test_lf_a22_doctor_lists_the_filters_and_a_healthy_index_adds_no_row(flog, tmp_path):
    from axiom.cli.ext.commands import doctor

    entry({"area": "North", "pressure": "4"})
    seqs(flog, q(field__area="North"))
    registry.reset()  # the extension's own copy of the log is the one doctor reads
    declared = doctor._check_logbooks_declared(_ext(tmp_path))
    assert "filters area, asset_id, pressure, note, checked, count, seen, due" in declared[0].detail
    assert doctor._check_logbook_filters(_ext(tmp_path / "again")) == []


@pytest.mark.acceptance("LF-A22")
def test_lf_a22_doctor_reports_each_problem_with_its_fix(flog, tmp_path, monkeypatch):
    from axiom.cli.ext.commands import doctor

    entry({"count": "many", "pressure": {"value": "60", "unit": "psi"}})
    seqs(flog, q(field__area="North"))
    with store.session_scope() as s:
        s.execute(text("UPDATE attest_field_values SET source_digest = 'x' WHERE field = 'count'"))
        s.commit()
    monkeypatch.setattr(field_index, "after_append", lambda *a, **k: None)
    monkeypatch.setattr(field_index, "CATCH_UP_BOUND", 1)
    entry({"area": "North"})
    entry({"area": "South"})
    found = doctor._check_logbook_filters(_ext(tmp_path))
    words = " | ".join(f"{r.detail} -> {r.remediation}" for r in found)
    assert all(r.check == "logbook.filters" and not r.ok for r in found)
    assert "2 records behind the chain head" in words
    assert "1 value of count could only be indexed as text" in words
    assert "1 value of pressure is in a unit no filter can compare (psi)" in words
    assert "1 row no longer matches its record's digest" in words
    assert "log filters reindex --site site-a" in words


# -- LF-A23: the cross-door matrix has rows for field.<id> and for the filters door -----------


@pytest.mark.acceptance("LF-A23")
def test_lf_a23_the_door_registry_carries_the_filters_door_and_the_field_params():
    from axiom.extensions.builtins.attest import doors  # noqa: F401 - registers the log's doors
    from axiom.governance import doors as g

    from .test_ld1 import ROWS

    http, skill = g.get("attest.http.filters"), g.get("attest.filters")
    assert http.route == ("GET", "/api/v1/attest/{logbook}/filters") and http.serves == "filters"
    assert skill.skill == "attest.filters"
    for door_id in ("attest.http.records", "attest.http.export_create", "attest.http.export_preview",
                    "attest.show", "attest.export", "attest.export_preview"):  # fmt: skip
        assert {"field.<id>", "where"} <= set(g.get(door_id).params), door_id
    # The generated matrix (LD-1) drives both new doors through every reason on every transport.
    driven = {p.values[0].id for p in ROWS}
    assert {"attest.http.filters", "attest.filters"} <= driven
