# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit LD-8: summary-first retrieval over archives (#1765, ADR-251 D7).

The world is LD-5's: record #1 (LD-1's round check, carrying a note nobody else has), record #2
(a photo attached) and the range seal #3 over both, archived and retired. The index uses the
built-in hashing embedder, so every vector is deterministic and made on this machine.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import (  # noqa: E402
    archives,
    index,
    registry,
    retention,
    summary_index,
)
from axiom.extensions.builtins.attest import doors as log_doors  # noqa: E402
from axiom.governance import doors as g  # noqa: E402
from axiom.governance.doors import Reason  # noqa: E402

from . import test_ld1 as ld1  # noqa: E402
from . import test_ld5 as ld5  # noqa: E402
from . import test_ld6 as ld6mod  # noqa: E402
from .test_ld1 import LOG, ME, SECRET_NOTE, SITE  # noqa: E402

world = ld1.world
restore_log = ld1.restore_log
blob_store = ld5.blob_store
arch_world = ld5.arch
ld6_world = ld6mod.ld6


@pytest.fixture
def ld6(ld6_world, monkeypatch):
    monkeypatch.setenv(index.PIN_ENV, "hashing")
    index.set_registry(None)
    index.refresh([(SITE, LOG)])
    yield ld6_world
    index.set_registry(None)

ROOT = Path(__file__).resolve().parents[2]
#: A word only record #1 holds (in its note), so a query for it can match nothing else.
ONLY_IN_ONE = "nobody"
LABELS = {"classification": "internal", "export_controlled": False}


@pytest.fixture
def arch(arch_world, monkeypatch):
    monkeypatch.setenv(index.PIN_ENV, "hashing")
    index.set_registry(None)
    index.refresh([(SITE, LOG)])
    yield arch_world
    index.set_registry(None)


def me(w, handle=ME) -> log_doors.LogCaller:
    return log_doors.LogCaller(handle=handle, sites=frozenset({SITE}), state_dir=w["state"])


def rows() -> dict[int, dict]:
    return {r["seq"]: r for r in index.rows_for_tests() if r["logbook"] == LOG}


def search(w, query=ONLY_IN_ONE, handle=ME) -> dict:
    return index.search(me(w, handle), query, site=SITE, logbook=LOG, state_dir=w["state"])


def rehydrate(w, archive_id, handle=ME, surface="cli"):
    return ld5._skill(w, "attest.rehydrate", {"site": SITE, "logbook": LOG, "archive_id": archive_id},
                      handle=handle, surface=surface)  # fmt: skip


def _reads(w, door_id):
    return ld5._reads(w, door_id)


# -- LD-8-A1: no vector row for the period's entries; one labelled summary chunk per seal ---------


@pytest.mark.acceptance("LD-8-A1")
def test_ld8_a1_after_retire_one_summary_chunk_per_seal_remains_with_the_periods_labels(arch):
    seal = arch["seal"]
    assert set(rows()) == {1, 2, seal["seq"]}  # hot: one chunk per record
    ld5._retired(arch)
    index.refresh([(SITE, LOG)])
    got = rows()
    assert set(got) == {seal["seq"]}, got  # no row at all remains for the period's entries
    chunk = got[seal["seq"]]
    assert chunk["attestation_id"] == seal["attestation_id"] and chunk["kind"] == "seal"
    assert {k: chunk[k] for k in LABELS} == LABELS == archives.manifest(
        archives.listed(SITE, LOG)[0]["archive_id"])["labels"]  # fmt: skip
    assert chunk["embedding"] is not None and chunk["warm_until"] is None
    # The summary's own words are indexed (not only the seal's title).
    assert {"two", "rounds", "order"} <= set(index.lexemes_for_tests(seal["attestation_id"]))
    # Drop and rebuild keeps it so: one summary, no fine rows, the same digest every time.
    first = index.rebuild()
    assert set(rows()) == {seal["seq"]} and index.rebuild() == first


@pytest.mark.acceptance("LD-8-A1")
def test_ld8_a1_a_summary_chunk_without_labels_is_refused(arch):
    seal_row = {**arch["seal"], "site_id": SITE, "logbook": LOG}
    with pytest.raises(index.IndexRefused, match="no labels"):
        index.chunk_for(seal_row, labels=None, roles=("*",))
    with pytest.raises(index.IndexRefused, match="no labels"):
        index.chunk_for(seal_row, labels={"classification": "internal"}, roles=("*",))


@pytest.mark.acceptance("LD-8-A1")
def test_ld8_a1_an_archived_but_not_retired_period_keeps_its_fine_chunks(arch):
    ld5._committed(arch)  # reads come from the package; nothing reclaimed until a person retires
    index.refresh([(SITE, LOG)])
    assert set(rows()) == {1, 2, arch["seal"]["seq"]}
    assert all(r["embedding"] is not None for r in rows().values())


# -- LD-8-A2: a query matching an archived entry returns the summary, marked archived -------------


@pytest.mark.acceptance("LD-8-A2")
def test_ld8_a2_a_query_matching_an_archived_entry_returns_the_summary_marked_archived(arch):
    hot = search(arch)
    assert [r["attestation_id"] for r in hot["results"]] == [arch["rec"]["attestation_id"]]
    a = ld5._retired(arch)
    got = search(arch)
    seal = arch["seal"]
    assert [r["attestation_id"] for r in got["results"]] == [seal["attestation_id"]], got
    hit = got["results"][0]
    assert hit["archived"] is True and hit["summary"] is True and hit["kind"] == "seal"
    assert hit["period"] == {"archive_id": a["archive_id"], "from_seq": 1, "to_seq": 2,
                             "seal_seq": seal["seq"]}  # fmt: skip
    assert hit["link"] == f"/api/v1/attest/{LOG}/records/{seal['attestation_id']}?site={SITE}"
    assert hit["url"] == f"#/log/{LOG}/{seal['attestation_id']}"
    assert hit["source"] == f"archive:sha256:{a['package_digest']}"
    assert hit["as_of"] is not None
    # The entry's content is not in the result: only the seal's own title and summary.
    shown = json.dumps(got["results"])
    for leak in (SECRET_NOTE, "Round check", '"reading"', arch["rec"]["digest"]):
        assert leak not in shown, leak
    assert ld5.SUMMARY in hit["snippet"]
    # The read record names the summary served, and the package it came from.
    rec = _reads(arch, "attest.search")[-1]["access"]
    assert rec["ids"] == [seal["attestation_id"]] and rec["source"] == hit["source"]


@pytest.mark.acceptance("LF-A27")
def test_lf_a27_a_summary_never_answers_a_field_filtered_search(arch, monkeypatch):
    """A summary's words are folded from every entry of its period, so under a field filter it
    cannot vouch that the entry it would surface is one the filter admits: even when the filter
    admits the seal itself, the summary is not a candidate and the filtered-out entry's words
    never surface through it (LF-A27 with unit LD-8)."""
    from axiom.extensions.builtins.attest import field_index

    ld5._retired(arch)
    assert [r["attestation_id"] for r in search(arch)["results"]] == [arch["seal"]["attestation_id"]]
    seal_seq = arch["seal"]["seq"]
    # A filter that admits the seal's own entry and nothing else of the period.
    monkeypatch.setattr(index, "_field_predicates", lambda fields, logbook: ("seal-only",))
    monkeypatch.setattr(field_index, "catch_up", lambda site, logbook: 0)
    monkeypatch.setattr(field_index, "search_clause", lambda row, p: row.c.seq == seal_seq)
    got = index.search(me(arch), ONLY_IN_ONE, site=SITE, logbook=LOG, state_dir=arch["state"],
                       fields={"field.any": "x"})  # fmt: skip
    assert got["results"] == [] and got["count"] == 0, got


@pytest.mark.acceptance("LD-8-A2")
def test_ld8_a2_a_search_read_record_names_only_the_package_this_search_read(arch):
    """Each search is a new read: a package an earlier read in the same context came from is not
    named as this search's source (the intermittent A2 failure under xdist, found 2026-11-21)."""
    archives.note_source("f" * 64)  # an earlier read, in this context, from some other package
    search(arch)
    assert _reads(arch, "attest.search")[-1]["access"].get("source") is None
    a = ld5._retired(arch)
    archives.note_source("f" * 64)
    search(arch)
    assert _reads(arch, "attest.search")[-1]["access"]["source"] == f"archive:sha256:{a['package_digest']}"
    # And a memory reference resolved outside a skill (LD-3's standalone door) likewise.
    from axiom.extensions.builtins.attest import references as log_refs

    archives.note_source("f" * 64)
    got = log_refs.LogResolver().resolve(  # the hot record #1 is inside the retired period
        {"id": arch["rec"]["attestation_id"], "digest": arch["rec"]["digest"], "site": SITE, "scope": LOG},
        caller=me(arch), transport="mcp")  # fmt: skip
    assert got.allowed
    assert _reads(arch, "attest.reference")[-1]["access"]["source"] == f"archive:sha256:{a['package_digest']}"


@pytest.mark.acceptance("LD-8-A2")
def test_ld8_a2_a_hot_result_is_not_marked_archived(arch):
    got = search(arch)
    assert got["results"] and got["results"][0]["archived"] is False
    assert "period" not in got["results"][0] and got["results"][0]["summary"] is False


# -- LD-8-A3: rehydrate through the door, recorded, warm with the same labels --------------------


@pytest.mark.acceptance("LD-8-A3")
def test_ld8_a3_rehydrate_returns_the_period_from_the_package_through_the_door(arch):
    a = ld5._retired(arch)
    before = len(_reads(arch, "attest.rehydrate"))
    got = rehydrate(arch, a["archive_id"])
    assert got.ok, got.errors
    v = got.value
    assert [r["seq"] for r in v["records"]] == [1, 2, arch["seal"]["seq"]]
    assert v["records"][0] == arch["rec"] and v["records"][-1] == arch["seal"]  # as chained
    assert v["period"]["archive_id"] == a["archive_id"] and v["warm_until"]
    assert v["source"] == f"archive:sha256:{a['package_digest']}"
    # One read record, naming the archive as the source and the records served.
    recorded = _reads(arch, "attest.rehydrate")[before:]
    assert len(recorded) == 1 and recorded[0]["outcome"] == "ok"
    acc = recorded[0]["access"]
    assert acc["source"] == v["source"] and acc["labels"] == LABELS and acc["what"] == "rehydrate"
    assert acc["ids"] == [r["attestation_id"] for r in v["records"]]
    assert SECRET_NOTE not in json.dumps(recorded)  # a read record never holds content


@pytest.mark.acceptance("LD-8-A3")
def test_ld8_a3_rehydrate_reindexes_the_period_warm_with_the_same_labels(arch):
    a = ld5._retired(arch)
    assert rehydrate(arch, a["archive_id"]).ok
    got = rows()
    warm = {s: r for s, r in got.items() if r["warm_until"] is not None}
    assert set(warm) == {1, 2}
    for r in warm.values():
        assert {k: r[k] for k in LABELS} == LABELS and r["embedding"] is not None
    assert got[arch["seal"]["seq"]]["warm_until"] is None  # the summary is not a warm row
    # A search now finds the entry itself (from the package, marked archived) and the summary.
    found = search(arch)
    by_id = {r["attestation_id"]: r for r in found["results"]}
    hit = by_id[arch["rec"]["attestation_id"]]
    assert hit["archived"] is True and hit["summary"] is False and SECRET_NOTE.split()[0] in hit["snippet"]
    assert hit["source"] == f"archive:sha256:{a['package_digest']}"
    # Rehydrating twice does not double anything.
    assert rehydrate(arch, a["archive_id"]).ok
    assert set(rows()) == {1, 2, arch["seal"]["seq"]}


@pytest.mark.acceptance("LD-8-A3")
def test_ld8_a3_rehydration_outside_a_door_is_refused_and_nothing_is_warmed(arch):
    a = ld5._retired(arch)
    log_doors.clear()
    with pytest.raises(archives.ArchiveRefused, match="door"):
        summary_index.rehydrate(a["archive_id"])
    assert set(rows()) == {arch["seal"]["seq"]}
    # A door admitted for another log is not a door for this archive.
    other = replace(registry.get(LOG), id="other_log", handle=None)
    registry.register(other)
    try:
        decision = log_doors.check("attest.rehydrate", me(arch), log_doors.target(SITE, "other_log"),
                                   transport="cli")  # fmt: skip
        assert decision.allowed
        with pytest.raises(archives.ArchiveRefused, match="door"):
            summary_index.rehydrate(a["archive_id"])
    finally:
        log_doors.clear()
        registry._logbooks.pop("other_log", None)  # noqa: SLF001
    assert set(rows()) == {arch["seal"]["seq"]}


@pytest.mark.acceptance("LD-8-A3")
def test_ld8_a3_an_archive_of_another_log_answers_as_not_found(arch):
    a = ld5._retired(arch)
    other = replace(registry.get(LOG), id="other_log", handle=None)
    registry.register(other)
    try:
        got = ld5._skill(arch, "attest.rehydrate",
                         {"site": SITE, "logbook": "other_log", "archive_id": a["archive_id"]})  # fmt: skip
        assert not got.ok and SECRET_NOTE not in json.dumps(got.value)
        assert "no archived period" in got.errors[0]
    finally:
        registry._logbooks.pop("other_log", None)  # noqa: SLF001
    assert set(rows()) == {arch["seal"]["seq"]}


# -- LD-8-A4: warm rows reaped after the declared window; the summary remains ------------------


@pytest.mark.acceptance("LD-8-A4")
def test_ld8_a4_warm_rows_are_reaped_after_the_declared_window_and_the_summary_remains(arch, monkeypatch):
    monkeypatch.setenv(summary_index.WARM_ENV, "2")  # hours
    a = ld5._retired(arch)
    t0 = summary_index._now()  # noqa: SLF001
    assert rehydrate(arch, a["archive_id"]).ok
    warm = [r for r in rows().values() if r["warm_until"] is not None]
    assert warm and all(abs((r["warm_until"] - t0) - timedelta(hours=2)) < timedelta(minutes=1) for r in warm)
    # Past the window, before any reaper ran: a search never ranks an expired warm row.
    later = t0 + timedelta(hours=2, minutes=1)
    monkeypatch.setattr(summary_index, "_now", lambda: later)
    got = search(arch)
    assert [r["attestation_id"] for r in got["results"]] == [arch["seal"]["attestation_id"]]
    # And the refresh that search ran reaped them: only the summary remains.
    assert set(rows()) == {arch["seal"]["seq"]}
    assert rows()[arch["seal"]["seq"]]["embedding"] is not None


@pytest.mark.acceptance("LD-8-A4")
def test_ld8_a4_the_reaper_runs_on_its_own_and_counts_what_it_reaped(arch, monkeypatch):
    a = ld5._retired(arch)
    assert rehydrate(arch, a["archive_id"]).ok
    assert summary_index.reap() == 0  # nothing is past its window yet
    later = summary_index._now() + summary_index.warm_window() + timedelta(seconds=1)  # noqa: SLF001
    monkeypatch.setattr(summary_index, "_now", lambda: later)
    assert summary_index.reap() == 2
    assert set(rows()) == {arch["seal"]["seq"]}


@pytest.mark.acceptance("LD-8-A4")
def test_ld8_a4_the_window_cannot_be_asked_for_by_a_caller(arch):
    a = ld5._retired(arch)
    got = ld5._skill(arch, "attest.rehydrate", {"site": SITE, "logbook": LOG, "archive_id": a["archive_id"],
                                                "keep_hours": 10_000})  # fmt: skip
    assert not got.ok or all(r["warm_until"] is None or r["warm_until"] - summary_index._now()  # noqa: SLF001
                             <= summary_index.warm_window() for r in rows().values())  # fmt: skip


# -- LD-8-A5: denial for an archived period is identical to denial for a hot one, every door -------


@pytest.mark.acceptance("LD-8-A5")
@pytest.mark.parametrize("reason", list(Reason), ids=lambda r: r.value)
def test_ld8_a5_denial_for_an_archived_period_is_the_hot_denial_on_every_door(
    reason, arch, restore_log, monkeypatch, tmp_path
):
    a = ld5._retired(arch)
    who = ld1.arrange(reason, monkeypatch, arch["state"])
    status, code = g.WIRE[reason]
    for door in (d for d in g.doors() if d.shape == "target"):
        for transport in door.transports:
            if transport == g.HTTP:
                got_status, body = ld1.drive_http(door, arch, who)
                assert (got_status, (body or {}).get("code")) == (status, code), (door.id, body)
                assert set(body) == {"detail", "code"}, body
                said = json.dumps(body)
            else:
                result = ld1.drive_skill(door, transport, arch, who, tmp_path)
                assert not result.ok and result.value == g.Decision(False, reason).body(), (door.id, result)
                said = json.dumps(result.value) + " ".join(result.errors)
            for leak in (*ld1.CONTENT, arch["rec"]["digest"], a["package_digest"], ld5.SUMMARY):
                assert leak not in said, (door.id, leak)
    # No denial names the package it would have read from.
    denied = [r["access"] for r in ld5.audit_trail.read_chain(ld5.audit_trail.audit_path(arch["state"]))
              if r.get("outcome") == "denied" and r.get("access")]  # fmt: skip
    assert denied and all(d.get("source") is None for d in denied)


@pytest.mark.acceptance("LD-8-A5")
def test_ld8_a5_the_rehydrate_door_is_a_row_of_the_one_registry(arch):
    door = g.get("attest.rehydrate")
    assert door.extension == "attest" and door.skill == "attest.rehydrate" and door.shape == "target"
    # It writes warm index rows, so it is not offered on MCP (software may not write through it).
    assert set(door.transports) == {"cli"}
    from axiom.extensions.builtins.attest.skills import SPECS

    spec = next(s for s in SPECS if s.name == "attest.rehydrate")
    assert (spec.side_effects, spec.risk, spec.surfaces) == (True, "reversible-write", ("cli",))
    assert [p.id for p in spec.requires] == ["archive-named"]
    assert "archive_id" in door.params


@pytest.mark.acceptance("LD-8-A5")
def test_ld8_a5_a_refused_reader_finds_no_summary_and_no_count(arch, monkeypatch):
    ld5._retired(arch)
    monkeypatch.setattr("axiom.extensions.builtins.attest.access_requests.knowable", lambda *_a, **_k: False)
    registry.register(replace(registry.get(LOG), readers=("auditor",)))
    try:
        got = search(arch, handle=ld5.OTHER)
        assert got["results"] == [] and got["count"] == 0
        got = search(arch, query="rounds order", handle=ld5.OTHER)
        assert got["results"] == [] and got["count"] == 0
    finally:
        registry.register(replace(registry.get(LOG), readers=()))


# -- tombstones: a removed record answers as removed, never summarised from stale data ---------


@pytest.mark.acceptance("LD-8-A2")
@pytest.mark.acceptance("LD-8-A3")
def test_ld8_a_tombstoned_record_is_never_summarised_or_rehydrated_from_stale_data(ld6, monkeypatch):
    """A real LD-6 tombstone over the retired period: once committed (reads hide the content) the
    summary no longer answers for the covered records' words and rehydrate answers them as
    removed; once erased (content and package gone) the summary stays, folded from what remains,
    and names no package."""
    gone = (ld6["rec"]["attestation_id"], ld6["rec2"]["attestation_id"])
    seal = ld6["seal"]
    assert [r["attestation_id"] for r in search(ld6)["results"]] == [seal["attestation_id"]]
    assert ONLY_IN_ONE in index.lexemes_for_tests(seal["attestation_id"])
    ld6mod._past_due(monkeypatch)
    t = ld6mod._tombstone(ld6).record
    retention.commit(t["attestation_id"], owner=ME)
    # Hidden: nothing matches the covered words, not even through the summary.
    got = search(ld6)
    assert got["results"] == [] and got["count"] == 0
    assert ONLY_IN_ONE not in index.lexemes_for_tests(seal["attestation_id"])
    # Rehydrate answers both as removed: digest kept, no content, nothing warmed.
    out = rehydrate(ld6, ld6["archive_id"])
    assert out.ok, out.errors
    by_id = {r["attestation_id"]: r for r in out.value["records"]}
    for aid in gone:
        assert by_id[aid]["removed"] is True and "content" not in by_id[aid] and "evidence" not in by_id[aid]
    assert by_id[gone[0]]["digest"] == ld6["rec"]["digest"]
    assert SECRET_NOTE not in json.dumps(out.value)
    assert not [r for r in rows().values() if r["warm_until"] is not None]
    # Erased: the package is gone; the summary remains, says it is one, and names no package.
    retention.erase(t["attestation_id"], approver=ME, override_soak="test: no soak")
    assert archives.get(ld6["archive_id"])["status"] == "tombstoned"
    assert search(ld6)["results"] == []
    left = search(ld6, query="rounds")["results"]
    assert [r["attestation_id"] for r in left] == [seal["attestation_id"]]
    assert left[0]["summary"] is True and left[0]["archived"] is True and left[0]["source"] is None
    assert ld6["package_digest"] not in json.dumps(left)
    assert set(rows()) >= {seal["seq"]} and not set(gone) & {r["attestation_id"] for r in rows().values()}
    refused = rehydrate(ld6, ld6["archive_id"])
    assert not refused.ok and SECRET_NOTE not in json.dumps(refused.value)


@pytest.mark.acceptance("LD-8-A2")
def test_ld8_a_gold_row_with_no_content_is_never_filled_from_the_package(arch):
    """A result or reference whose gold row has no content (removed) is served as removed: the
    package, which still holds the words, is not read to fill it."""
    ld5._retired(arch)
    row = {"attestation_id": arch["rec"]["attestation_id"], "site_id": SITE, "logbook": LOG,
           "seq": 1, "digest": arch["rec"]["digest"], "content": None}  # fmt: skip
    assert archives.overlay_gold(row)["content"] is None


# -- Must refuse --------------------------------------------------------------------------------


@pytest.mark.acceptance("LD-8-A2")
def test_ld8_refuses_a_parallel_archive_search_path():
    """One search (LD-2's) answers for archived periods too: no other attest module ranks or
    matches the index."""
    attest = ROOT / "src/axiom/extensions/builtins/attest"
    fusers = sorted(p.name for p in attest.rglob("*.py") if "/tests/" not in p.as_posix()
                    and "reciprocal_rank_fusion" in p.read_text(encoding="utf-8"))  # fmt: skip
    assert fusers == ["index.py"], fusers
    source = (attest / "summary_index.py").read_text(encoding="utf-8")
    for ranking in ("to_tsquery", "ts_rank", '"@@"', "_cosine", "reciprocal_rank_fusion"):
        assert ranking not in source, ranking
    assert not hasattr(summary_index, "search")


@pytest.mark.acceptance("LD-8-A1")
def test_ld8_the_summary_indexer_reads_gold_only():
    from axiom.governance import derived_sources

    path = ROOT / "src/axiom/extensions/builtins/attest/summary_index.py"
    assert summary_index.INDEX_SOURCES == ("gold.log_records",)
    assert derived_sources.declared_sources_ok(summary_index.INDEX_SOURCES) == []
    assert derived_sources.violations(path.read_text(encoding="utf-8")) == []
