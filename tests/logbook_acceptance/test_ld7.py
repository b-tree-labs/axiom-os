# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit LD-7: tiered gold (#1764, ADR-251 D7).

The world is LD-5's, on a clock set back to 2024: record #1 (LD-1's) and record #2 carrying a
photo, sealed by the range seal #3, archived, verified and committed. Then the clock moves to
2026 and record #4 is signed. The 2024 partition of the demo log is past its window (two years
are kept hot) and covered by a verified archive, so it may move to the cold tier; the 2026
partition holds #4, which no archive covers, so it may not.

A move is a switch on the ADR-197 runner: ``prepare`` builds the cold year table beside the hot
rows and copies them; the ``pre`` probe proves every row and every digest identical; ``commit``
attaches the cold table and removes the hot rows in one bounded transaction, its ``in_commit``
probe reading through the gold view; ``rollback`` moves the rows back. Readers never name a tier:
every read goes through the gold view or the chain table, through the same doors.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

fastapi = pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import (  # noqa: E402
    archives,
    gold,
    registry,
    service,
    store,
    tiers,
)
from axiom.extensions.builtins.attest.skills import bind_default  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, OP2, as_  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import _sign  # noqa: E402
from axiom.infra import audit_trail  # noqa: E402
from axiom.infra.principal import PrincipalContext  # noqa: E402
from axiom.infra.skill_dispatch import invoke_capability  # noqa: E402
from axiom.infra.skills import SkillContext  # noqa: E402
from axiom.infra.storage import provider_from_config  # noqa: E402
from axiom.infra.switchplan import (  # noqa: E402
    BudgetUnmeetable,
    GateNotPassed,
    LockBudget,
    ProbeFailed,
    SwitchRefused,
)

from . import test_ld1 as ld1  # noqa: E402
from . import test_ld4 as ld4  # noqa: E402
from . import test_ld5 as ld5  # noqa: E402
from .test_ld1 import LOG, ME, SITE  # noqa: E402

world = ld1.world
restore_log = ld1.restore_log
blob_store = ld4.blob_store

ROOT = Path(__file__).resolve().parents[2]
#: When the moves run: 2024 is two calendar years behind, so past a two-year hot window.
NOW = datetime(2026, 6, 1, 12, tzinfo=UTC)


@pytest.fixture
def clock(monkeypatch):
    """The chain's clock, set back to March 2024; each reading moves it on a minute."""
    t = {"now": datetime(2024, 3, 1, 9, tzinfo=UTC)}

    def now() -> datetime:
        t["now"] += timedelta(minutes=1)
        return t["now"]

    monkeypatch.setattr(service, "_now", now)
    return t


@pytest.fixture
def ld7(clock, world, restore_log, blob_store, node_signer, tmp_path, monkeypatch):
    """#1 and #2 (2024), sealed by #3 (2024), archived, verified and committed; #4 (2026)."""
    ld5._sealable()
    monkeypatch.setenv("AXI_CHANGE_LEDGER", str(tmp_path / "changes.jsonl"))
    monkeypatch.delenv("AXIOM_ATTEST_COLD_TABLESPACE", raising=False)
    monkeypatch.delenv("AXIOM_ATTEST_HOT_YEARS", raising=False)
    cold = tmp_path / "cold"
    archives.set_target(provider_from_config({"base_dir": str(cold)}))
    rec2 = ld4._signed(node_signer, (ld4.PHOTO, "image/png", {"name": "pump.png"}))
    draft = service.create_seal_draft(site_id=SITE, logbook=LOG, boundary="range", from_seq=1, to_seq=2,
                                      summary="Two rounds.", origin="human", for_principal=ME)  # fmt: skip
    seal = _sign(draft, node_signer).record
    built = archives.build(SITE, LOG, by=ME)
    clock["now"] = datetime(2026, 2, 1, 9, tzinfo=UTC)
    rec4 = ld4._signed(node_signer, note="after the seal")
    yield {**world, "rec2": rec2, "seal": seal, "rec4": rec4, "archive_id": built["archive_id"],
           "package_digest": built["package_digest"], "cold": cold, "tmp": tmp_path, "signer": node_signer,
           "clock": clock}  # fmt: skip
    archives.set_target(None)
    archives._packages.clear()  # noqa: SLF001


def _archived(w):
    archives.verify(w["archive_id"], by=ME)
    archives.commit(w["archive_id"], owner=ME)
    return archives.get(w["archive_id"])


def _ctx(state: Path, handle: str | None, surface: str = "cli") -> SkillContext:
    principal = PrincipalContext(handle=handle, posture="sso", assured=True) if handle else None
    return SkillContext(registry=bind_default(), state_dir=state, logger=logging.getLogger("ld7"),
                        principal=principal, actor=handle or "", surface=surface)  # fmt: skip


def _skill(w, name, params, handle=ME, surface="cli"):
    c = _ctx(w["state"], handle, surface)
    return invoke_capability(c.registry, name, params, c, surface=surface)


def _rows(where: str = "", **params):
    """Every chain row of the demo log, with the table that holds it (the test's own look)."""
    with store.session_scope() as s:
        # nosemgrep: avoid-sqlalchemy-text -- a test's fixed query; ``where`` is a literal in this file
        got = s.execute(text(
            "SELECT seq, digest, tableoid::regclass::text AS home, to_jsonb(r) - 'search_vector' AS body "
            "FROM attest_records r WHERE site_id = :s AND logbook = :l " + where + " ORDER BY seq"),
            {"s": SITE, "l": LOG, **params}).mappings().all()  # fmt: skip
    return [dict(r) for r in got]


def _hot_seqs():
    return [r["seq"] for r in _rows() if r["home"].split(".")[-1] == "attest_records"]


def _gold_digest(lo=1, hi=4):
    return gold.partition_digest(gold.partition(SITE, LOG, lo, hi))


def _moved(w, **kw):
    """The 2024 partition moved through the runner, its soak passed."""
    _archived(w)
    return tiers.move(SITE, LOG, 2024, owner=ME, now=NOW, **kw)


# -- LD-7-A1: a move leaves every row digest identical, proved before commit ----------------------


@pytest.mark.acceptance("LD-7-A1")
def test_ld7_a1_a_move_leaves_every_row_and_digest_identical(ld7):
    before = _rows()
    gold_before = _gold_digest()
    done = _moved(ld7)
    assert done["status"] == "cold" and (done["from_seq"], done["to_seq"], done["rows"]) == (1, 3, 3)
    after = _rows()
    assert [(r["seq"], r["digest"], r["body"]) for r in after] == [(r["seq"], r["digest"], r["body"]) for r in before]
    homes = {r["seq"]: r["home"].split(".")[-1] for r in after}
    assert homes[4] == "attest_records" and {homes[1], homes[2], homes[3]} == {done["table"]}
    assert done["table"] != "attest_records" and _gold_digest() == gold_before


@pytest.mark.acceptance("LD-7-A1")
def test_ld7_a1_the_runners_probe_checks_every_digest_before_commit(ld7):
    _archived(ld7)
    p = tiers.plan(SITE, LOG, 2024, owner=ME, now=NOW)
    p.prepare()
    assert _hot_seqs() == [1, 2, 3, 4]  # built beside: the hot rows are still the ones read
    results = p.prove()
    names = {r.name: r for r in results}
    assert names["rows-identical"].ok and "3 row(s)" in names["rows-identical"].detail
    assert names["gold-digest-identical"].ok
    # A staged copy that differs in one byte is caught by the same probe, and nothing moves.
    p.rollback("test: build the copy again")
    assert _hot_seqs() == [1, 2, 3, 4]
    p = tiers.plan(SITE, LOG, 2024, owner=ME, now=NOW)
    p.prepare()
    table = tiers.get_for(SITE, LOG, 2024)["table"]
    _tamper(table, seq=2)
    with pytest.raises(GateNotPassed, match="rows-identical"):
        p.prove()
    assert _hot_seqs() == [1, 2, 3, 4]


def _tamper(table: str, *, seq: int) -> None:
    """Change one staged row as a forger with the database owner's rights would (the guard off)."""
    t = gold.ident(table)
    with store.session_scope() as s:
        # nosemgrep: avoid-sqlalchemy-text -- a test's DDL; the table name passed ident()
        s.execute(text(f"ALTER TABLE {t} DISABLE TRIGGER USER"))
        # nosemgrep: avoid-sqlalchemy-text -- a test's forgery; the table name passed ident()
        s.execute(text(f"UPDATE {t} SET record = jsonb_set(record, '{{content,note}}', '\"forged\"') "
                       "WHERE seq = :q"), {"q": seq})  # fmt: skip
        # nosemgrep: avoid-sqlalchemy-text -- a test's DDL; the table name passed ident()
        s.execute(text(f"ALTER TABLE {t} ENABLE TRIGGER USER"))
        s.commit()


# -- LD-7-A2: a query across hot and cold returns the same rows, through the door, same as_of -----


def _door_views(w):
    c = w["client"]
    c.cookies.clear()
    page = as_(c, OP1).get(f"/api/v1/attest/{LOG}/records?site={SITE}")
    assert page.status_code == 200, page.text
    one = as_(c, OP1).get(f"/api/v1/attest/{LOG}/records/{w['rec']['attestation_id']}?site={SITE}")
    assert one.status_code == 200, one.text
    refs = {}
    for rec in (w["rec"], w["rec4"]):
        got = _skill(w, "attest.reference", {"attestation_id": rec["attestation_id"]})
        assert got.ok, got.errors
        refs[rec["seq"]] = got.value
    shown = _skill(w, "attest.show", {"site": SITE, "logbook": LOG})
    assert shown.ok, shown.errors
    return {"page": page.json(), "one": one.json(), "refs": refs, "shown": shown.value}


@pytest.mark.acceptance("LD-7-A2")
def test_ld7_a2_a_query_across_hot_and_cold_returns_the_same_rows_and_as_of(ld7):
    _archived(ld7)
    before = _door_views(ld7)
    assert {r["seq"] for r in before["page"]["records"]} >= {1, 2, 3, 4}
    tiers.move(SITE, LOG, 2024, owner=ME, now=NOW)
    after = _door_views(ld7)
    assert after == before
    assert after["refs"][1]["as_of"] == before["refs"][1]["as_of"] is not None
    # Every one of those reads went through its door and was recorded there.
    doors = {(r.get("access") or {}).get("door") for r in audit_trail.read_chain(audit_trail.audit_path(ld7["state"]))}
    assert {"attest.http.records", "attest.http.record", "attest.reference", "attest.show"} <= doors


@pytest.mark.acceptance("LD-7-A2")
def test_ld7_a2_a_cold_record_is_refused_by_the_same_check(ld7, monkeypatch):
    _moved(ld7)
    registry.register(replace(registry.get(LOG), readers=("auditor",)))
    monkeypatch.setattr("axiom.extensions.builtins.attest.access_requests.knowable", lambda *_a, **_k: False)
    c = ld7["client"]
    c.cookies.clear()
    for rec in (ld7["rec"], ld7["rec4"]):  # cold and hot: one answer
        r = as_(c, OP2).get(f"/api/v1/attest/{LOG}/records/{rec['attestation_id']}?site={SITE}")
        assert (r.status_code, r.json()["code"]) == (404, "not_found")
        assert "attest_records" not in r.text


@pytest.mark.acceptance("LD-7-A2")
def test_ld7_a2_the_chain_verifies_across_tiers(ld7):
    from axiom.attest import verify

    _moved(ld7)
    assert verify.chain(SITE, LOG).ok
    assert verify.chain(SITE, LOG, from_seq=3, to_seq=4).ok  # a slice from cold into hot
    got = _skill(ld7, "attest.verify", {"site": SITE, "logbook": LOG})
    assert got.ok and got.value["ok"], got


# -- LD-7-A3: through the runner, a lock budget and a change record; a failed probe rolls back ---


@pytest.mark.acceptance("LD-7-A3")
def test_ld7_a3_the_move_runs_through_the_runner_with_a_budget_and_a_change_record(ld7):
    done = _moved(ld7, budget=LockBudget(max_hold_s=5.0))
    rec = tiers.switch_record(done["move_id"])
    assert rec.kind == tiers.KIND and rec.phase in ("committed", "soaked", "retired")
    assert rec.budget["max_hold_s"] == 5.0 and rec.holds and rec.holds[0]["budget_s"] == 5.0
    assert not rec.holds[0]["breach"]
    steps = [e["step"] for e in rec.events]
    assert steps.index("prepare") < steps.index("prove") < steps.index("commit") < steps.index("committed")
    changes = [json.loads(x) for x in (ld7["tmp"] / "changes.jsonl").read_text().splitlines() if x.strip()]
    assert any(rec.change_id == (c.get("id") or c.get("change_id")) for c in changes)
    # The soak runs the post probes (the partition unchanged through gold, the chain verifying across
    # tiers); passing, it closes the rollback window. Nothing is destroyed, so no approver is asked.
    p = tiers.plan(SITE, LOG, 2024, owner=ME, now=NOW)
    assert p.soak_check() == "retired"
    assert tiers.get(done["move_id"])["status"] == "retired" and _hot_seqs() == [4]
    with pytest.raises(SwitchRefused):
        p.rollback("too late")


@pytest.mark.acceptance("LD-7-A3")
def test_ld7_a3_a_budget_the_move_cannot_meet_is_refused_before_the_lock(ld7):
    _archived(ld7)
    with pytest.raises(BudgetUnmeetable):
        tiers.move(SITE, LOG, 2024, owner=ME, now=NOW, budget=LockBudget(max_hold_s=1e-6))
    assert _hot_seqs() == [1, 2, 3, 4]


@pytest.mark.acceptance("LD-7-A3")
def test_ld7_a3_a_failed_in_commit_probe_rolls_back_and_nothing_moves(ld7):
    _archived(ld7)
    p = tiers.plan(SITE, LOG, 2024, owner=ME, now=NOW)
    p.prepare()
    p.prove()
    table = tiers.get_for(SITE, LOG, 2024)["table"]
    _tamper(table, seq=1)  # after the gate passed: only the probe inside the commit can see it
    before = _rows()
    with pytest.raises((ProbeFailed, Exception)) as err:
        p.commit()
    assert "refused" in str(err.value) or "probe" in str(err.value).lower() or "identical" in str(err.value)
    assert p.record.phase == "rolled_back"
    assert _hot_seqs() == [1, 2, 3, 4] and _rows() == before
    assert not tiers.table_exists(table)
    assert tiers.get(p.record.state["move_id"])["status"] == "rolled_back"


@pytest.mark.acceptance("LD-7-A3")
def test_ld7_a3_rollback_moves_the_rows_back_to_the_hot_tier(ld7):
    before, gold_before = _rows(), _gold_digest()
    _archived(ld7)
    p = tiers.plan(SITE, LOG, 2024, owner=ME, now=NOW, soak=timedelta(days=1))
    p.run()
    assert _hot_seqs() == [4]
    p.rollback("operator request")
    assert p.record.phase == "rolled_back" and _hot_seqs() == [1, 2, 3, 4]
    assert [(r["seq"], r["body"]) for r in _rows()] == [(r["seq"], r["body"]) for r in before]
    assert _gold_digest() == gold_before


# -- LD-7-A4: no verified archive, no move ----------------------------------------------------------


@pytest.mark.acceptance("LD-7-A4")
def test_ld7_a4_a_partition_whose_period_has_no_verified_archive_is_refused(ld7):
    # Packaged but never verified.
    with pytest.raises(tiers.TierRefused, match="verified archive"):
        tiers.move(SITE, LOG, 2024, owner=ME, now=NOW)
    assert _hot_seqs() == [1, 2, 3, 4] and tiers.listed(SITE, LOG) == []


@pytest.mark.acceptance("LD-7-A4")
def test_ld7_a4_a_partition_holding_an_unarchived_record_is_refused(ld7):
    _archived(ld7)
    later = datetime(2030, 1, 2, tzinfo=UTC)  # 2026 is past its window then, but #4 is in no archive
    with pytest.raises(tiers.TierRefused, match="#4"):
        tiers.move(SITE, LOG, 2026, owner=ME, now=later)
    assert _hot_seqs() == [1, 2, 3, 4]


@pytest.mark.acceptance("LD-7-A4")
def test_ld7_a4_a_partition_inside_its_window_stays_hot(ld7, monkeypatch):
    _archived(ld7)
    with pytest.raises(tiers.TierRefused, match="window"):
        tiers.move(SITE, LOG, 2024, owner=ME, now=datetime(2025, 6, 1, tzinfo=UTC))
    monkeypatch.setenv("AXIOM_ATTEST_HOT_YEARS", "3")
    with pytest.raises(tiers.TierRefused, match="window"):
        tiers.move(SITE, LOG, 2024, owner=ME, now=NOW)
    assert _hot_seqs() == [1, 2, 3, 4]


@pytest.mark.acceptance("LD-7-A4")
def test_ld7_a4_the_skill_moves_for_a_person_who_may_close_the_log_and_refuses_others(ld7, monkeypatch):
    _archived(ld7)
    monkeypatch.setattr(tiers, "_now", lambda: NOW)
    refused = _skill(ld7, "attest.tier_move", {"site": SITE, "logbook": LOG, "year": 2024}, handle="@nobody:site-a")
    assert not refused.ok and "close" in " ".join(refused.errors)
    got = _skill(ld7, "attest.tier_move", {"site": SITE, "logbook": LOG, "year": 2024})
    assert got.ok, got.errors
    assert got.value["status"] == "cold" and _hot_seqs() == [4]


# -- LD-7-A5: tier-audit reports partitions per tier and their windows -------------------------------


@pytest.mark.acceptance("LD-7-A5")
def test_ld7_a5_tier_audit_reports_partitions_per_tier_and_their_windows(ld7, real_postgres):
    from axiom.extensions.builtins.data_platform.skills import tier_audit

    done = _moved(ld7)
    assert _gold_digest()  # the gold view is in place, as on any node that serves the log
    got = tier_audit.run({"dsn": real_postgres.replace("+psycopg2", ""), "schemas": [gold.schema()]}, None)
    assert got.ok, got.errors
    parts = got.value["partitions"]
    ours = [p for p in parts if p["view"] == f"{gold.schema()}.{gold.VIEW}"]
    assert len(ours) == 1, parts
    tiers_seen = ours[0]["tiers"]
    assert set(tiers_seen) == {"hot", "cold"}
    assert [t["table"].split(".")[-1] for t in tiers_seen["hot"]] == ["attest_records"]
    cold = [t for t in tiers_seen["cold"] if t["table"].split(".")[-1] == done["table"]]
    assert len(cold) == 1
    assert cold[0]["window"] == {"from": "2024-01-01T00:00:00+00:00", "to": "2025-01-01T00:00:00+00:00"}
    assert cold[0]["rows"] == 3 and cold[0]["tablespace"] == "pg_default"
    # It names no log and no site: an operator's diagnostic is not a door to the log.
    assert LOG not in json.dumps(got.value) and SITE not in json.dumps(got.value)
    assert any("cold" in a for a in got.actions_taken)


@pytest.mark.acceptance("LD-7-A5")
def test_ld7_a5_the_logs_own_report_names_each_years_tier_and_window(ld7):
    _moved(ld7)
    rep = {p["year"]: p for p in tiers.partitions(SITE, LOG, now=NOW)}
    assert rep[2024]["tier"] == "cold" and rep[2026]["tier"] == "hot"
    assert rep[2024]["window"] == {"from": "2024-01-01T00:00:00+00:00", "to": "2025-01-01T00:00:00+00:00"}
    assert rep[2026]["eligible"] is False and "window" in rep[2026]["why_not"]


@pytest.mark.acceptance("LD-7-A5")
def test_ld7_a5_a_declared_cold_tablespace_must_exist(ld7, monkeypatch):
    _archived(ld7)
    monkeypatch.setenv("AXIOM_ATTEST_COLD_TABLESPACE", "no_such_space")
    with pytest.raises(tiers.TierRefused, match="no_such_space"):
        tiers.move(SITE, LOG, 2024, owner=ME, now=NOW)
    monkeypatch.setenv("AXIOM_ATTEST_COLD_TABLESPACE", "pg_default")
    assert tiers.move(SITE, LOG, 2024, owner=ME, now=NOW)["tablespace"] == "pg_default"


# -- Must refuse ------------------------------------------------------------------------------------


@pytest.mark.acceptance("LD-7-A2")
def test_ld7_refuses_page_local_sql_to_reach_a_cold_partition():
    """No module but the tier's own builds a cold table's name; every reader names the chain or the
    gold view, and so reads through the one door whatever tier holds the row."""
    pattern = re.compile(re.escape(tiers.PREFIX))
    offenders = []
    for path in (ROOT / "src").rglob("*.py"):
        if path.name == "tiers.py" and path.parent.name == "attest":
            continue
        if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


@pytest.mark.acceptance("LD-7-A1")
def test_ld7_refuses_mutation_of_rows_during_a_move(ld7):
    _archived(ld7)
    p = tiers.plan(SITE, LOG, 2024, owner=ME, now=NOW)
    p.prepare()
    with store.session_scope() as s, pytest.raises(Exception, match="being moved"):
        s.execute(text("UPDATE attest_records SET record = record - 'content' WHERE site_id = :s AND logbook = :l "
                       "AND seq = 2"), {"s": SITE, "l": LOG})  # fmt: skip
    table = tiers.get_for(SITE, LOG, 2024)["table"]
    t = gold.ident(table)
    with store.session_scope() as s, pytest.raises(Exception, match="refused"):
        # nosemgrep: avoid-sqlalchemy-text -- a test's write; the table name passed ident()
        s.execute(text(f"INSERT INTO {t} (attestation_id, site_id, logbook, seq, digest, prev_digest, kind, "
                       "meaning, signer, occurred_at, recorded_at, record) SELECT 'x', site_id, logbook, 9, 'y', "
                       f"prev_digest, kind, meaning, signer, occurred_at, recorded_at, record FROM {t} LIMIT 1"))  # fmt: skip


@pytest.mark.acceptance("LD-7-A3")
def test_ld7_refuses_a_drop_in_place_of_a_move(ld7):
    """A hot row goes only when an identical copy sits in the cold table its committing move names;
    a cold row goes only back to the hot tier, by a rollback; a cold table is never truncated."""
    _archived(ld7)
    with store.session_scope() as s, pytest.raises(Exception, match="refused"):
        s.execute(text("DELETE FROM ONLY attest_records WHERE site_id = :s AND logbook = :l AND seq = 1"),
                  {"s": SITE, "l": LOG})  # fmt: skip
    p = tiers.plan(SITE, LOG, 2024, owner=ME, now=NOW)
    p.prepare()  # a copy exists now, but no move is committing: still refused
    with store.session_scope() as s, pytest.raises(Exception, match="being moved"):
        s.execute(text("DELETE FROM ONLY attest_records WHERE site_id = :s AND logbook = :l AND seq = 1"),
                  {"s": SITE, "l": LOG})  # fmt: skip
    p.prove()
    p.commit()
    table = gold.ident(tiers.get_for(SITE, LOG, 2024)["table"])
    for stmt in (f"DELETE FROM {table} WHERE seq = 1", f"TRUNCATE {table}",
                 f"UPDATE {table} SET signer = 'someone' WHERE seq = 1"):  # fmt: skip
        with store.session_scope() as s, pytest.raises(Exception, match="refused"):
            # nosemgrep: avoid-sqlalchemy-text -- a test's write; the table name passed ident()
            s.execute(text(stmt))
    assert [r["seq"] for r in _rows()] == [1, 2, 3, 4]
    assert tiers.drop_cold_refused(SITE, LOG, 2024)


@pytest.mark.acceptance("LD-7-A1")
def test_ld7_refuses_a_second_copy_of_a_moved_record_in_the_hot_tier(ld7):
    _moved(ld7)
    with store.session_scope() as s, pytest.raises(Exception, match="refused"):
        s.execute(text(
            "INSERT INTO attest_records (attestation_id, site_id, logbook, seq, digest, prev_digest, kind, meaning, "
            "signer, occurred_at, recorded_at, record) SELECT 'copy-1', site_id, logbook, seq, left('z' || digest, 64), "
            "prev_digest, kind, meaning, signer, occurred_at, recorded_at, record FROM attest_records "
            "WHERE site_id = :s AND logbook = :l AND seq = 1"), {"s": SITE, "l": LOG})  # fmt: skip
    with store.session_scope() as s, pytest.raises(Exception, match="refused"):
        s.execute(text(
            "INSERT INTO attest_records (attestation_id, site_id, logbook, seq, digest, prev_digest, kind, meaning, "
            "signer, occurred_at, recorded_at, record) SELECT attestation_id || '', site_id, logbook, 99, digest, "
            "prev_digest, kind, meaning, signer, occurred_at, recorded_at, record FROM attest_records "
            "WHERE site_id = :s AND logbook = :l AND seq = 2"), {"s": SITE, "l": LOG})  # fmt: skip


@pytest.mark.acceptance("LD-7-A4")
def test_ld7_refuses_a_move_without_an_archive(ld7):
    with store.session_scope() as s:
        s.execute(text("DELETE FROM attest_archives WHERE archive_id = :a"), {"a": ld7["archive_id"]})
        s.commit()
    with pytest.raises(tiers.TierRefused, match="verified archive"):
        tiers.move(SITE, LOG, 2024, owner=ME, now=NOW)
    assert _hot_seqs() == [1, 2, 3, 4]


@pytest.mark.acceptance("LD-7-A2")
def test_ld7_a_tombstone_erases_content_in_the_cold_tier_and_the_chain_still_verifies(ld7, monkeypatch):
    """Labels and retention follow the data: LD-6's erasure reaches a cold row by the same rule."""
    from axiom.attest import verify
    from axiom.extensions.builtins.attest import retention, roles

    from . import test_ld6 as ld6

    ld6._ruled()
    roles.grant(ME, SITE, ld6.CUSTODIAN, by="@admin:site-a", state_dir=ld7["tmp"])
    _moved(ld7)
    w = {**ld7}
    t = ld6._tombstone(w).record
    monkeypatch.setattr(retention, "_now", lambda: datetime.now(UTC) + timedelta(days=400))
    retention.commit(t["attestation_id"], owner=ME)
    retention.erase(t["attestation_id"], approver=ME, override_soak="test: no soak")
    rows = {r["seq"]: r for r in _rows()}
    assert "content" not in rows[1]["body"]["record"] and rows[1]["home"].split(".")[-1] != "attest_records"
    assert verify.chain(SITE, LOG).ok
