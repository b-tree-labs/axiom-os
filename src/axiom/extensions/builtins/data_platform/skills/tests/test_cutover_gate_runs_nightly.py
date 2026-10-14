# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi data cutover-gate``: the nightly comparison and the verdict, from a terminal or a timer.

Run once a day, it compares yesterday (UTC) on the old and new copies, records
the day in the gate's ledger, and prints the verdict with every unmet
condition. The same verb records an event (an outage, an upgrade) and the
one-off history check. The database addresses come from the vault, so a
password never reaches argv, the ledger or the output. Running it never changes
either copy.
"""

from __future__ import annotations

import hashlib
import json
import logging

import psycopg
import pytest

from axiom.extensions.builtins.data_platform.conformance import SILVER_SIGNALS_DDL, pg_upsert
from axiom.extensions.builtins.data_platform.conformance.cutover_gate import GateLedger
from axiom.extensions.builtins.data_platform.conformance.tests.conftest import (
    timescale_dsn,  # noqa: F401
)
from axiom.extensions.builtins.data_platform.skills import cutover_gate
from axiom.infra.skills import SkillContext, SkillRegistry

DAY = "2026-11-02"


@pytest.fixture
def copies(timescale_dsn, monkeypatch):  # noqa: F811
    conns = []
    for env, name in (("GATE_TEST_OLD_DSN", "verb_old"), ("GATE_TEST_NEW_DSN", "verb_new")):
        with psycopg.connect(timescale_dsn, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            admin.execute(f"CREATE DATABASE {name}")
        dsn = timescale_dsn.rsplit("/", 1)[0] + "/" + name
        monkeypatch.setenv(env, dsn)
        c = psycopg.connect(dsn, autocommit=True)
        for stmt in SILVER_SIGNALS_DDL:
            c.execute(stmt)
        conns.append(c)
    yield conns
    for c in conns:
        c.close()


def _rows(n: int, day: str = DAY) -> list[dict]:
    out = []
    for i in range(n):
        ts = f"{day}T08:00:{i:02d}+00:00"
        out.append({"site": "site-a", "feed": "site-a.live", "channel": "TC1", "ts": ts, "value": float(i),
                    "unit": "degC", "quality": "good", "source_class": "measured", "schema_ref": "site-a/v1",
                    "row_hash": hashlib.sha256(ts.encode()).hexdigest()})
    return out


def _load(conn, rows):
    up = pg_upsert(conn.cursor())
    for r in rows:
        up(r)


def _ctx(tmp_path):
    return SkillContext(registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("t"))


def _params(tmp_path, **kw):
    return {"old_ref": "env://GATE_TEST_OLD_DSN", "new_ref": "env://GATE_TEST_NEW_DSN", "sites": "site-a",
            "ledger": str(tmp_path / "gate.jsonl"), **kw}


def test_a_nightly_run_records_the_day_and_says_why_it_is_not_yet_safe(copies, tmp_path):
    old, new = copies
    _load(old, _rows(20))
    _load(new, _rows(20))
    result = cutover_gate.run(_params(tmp_path, day=DAY), _ctx(tmp_path))
    assert result.ok, result.errors
    assert result.value["day"]["clean"] is True
    assert result.value["verdict"]["safe"] is False
    assert any("history" in r for r in result.value["verdict"]["reasons"])
    assert [d.day for d in GateLedger(tmp_path / "gate.jsonl").days()] == [DAY]
    # Neither password nor address is echoed anywhere it could be kept.
    text = json.dumps(result.value) + " ".join(result.actions_taken) + (tmp_path / "gate.jsonl").read_text()
    assert "postgres:" not in text and "127.0.0.1" not in text


def test_a_disagreement_is_reported_and_nothing_is_changed(copies, tmp_path):
    old, new = copies
    _load(old, _rows(20))
    _load(new, _rows(19))
    result = cutover_gate.run(_params(tmp_path, day=DAY), _ctx(tmp_path))
    assert result.ok and result.value["day"]["clean"] is False
    assert result.value["day"]["missing_on_new"] == 1
    assert [old.execute("SELECT count(*) FROM silver.signals").fetchone()[0],
            new.execute("SELECT count(*) FROM silver.signals").fetchone()[0]] == [20, 19]


def test_events_and_history_are_recorded_without_comparing(tmp_path):
    ctx = _ctx(tmp_path)
    p = {"ledger": str(tmp_path / "gate.jsonl")}
    assert cutover_gate.run({**p, "event": "outage", "event_day": DAY, "note": "edge down 2 h"}, ctx).ok
    assert cutover_gate.run({**p, "history": "clean"}, ctx).ok
    ledger = GateLedger(tmp_path / "gate.jsonl")
    assert ledger.events() == [{"kind": "outage", "day": DAY, "note": "edge down 2 h"}]
    assert ledger.history_clean() is True
    verdict_only = cutover_gate.run({**p, "verdict_only": True}, ctx)
    assert verdict_only.ok and "day" not in verdict_only.value


def test_bad_input_is_refused_before_anything_is_touched(tmp_path):
    ctx = _ctx(tmp_path)
    assert not cutover_gate.run({"ledger": str(tmp_path / "g.jsonl"), "sites": "site-a"}, ctx).ok  # no refs
    assert not cutover_gate.run({"ledger": str(tmp_path / "g.jsonl"), "history": "maybe"}, ctx).ok
    assert not cutover_gate.run({"ledger": str(tmp_path / "g.jsonl"), "event": "outage", "event_day": "soon"}, ctx).ok
    assert not (tmp_path / "g.jsonl").exists()


def test_the_verb_reaches_the_skill_from_the_command_line(tmp_path, capsys):
    from axiom.extensions.builtins.data_platform.cli import main

    code = main(["--json", "cutover-gate", "--ledger", str(tmp_path / "gate.jsonl"), "--event", "upgrade",
                 "--event-day", DAY])
    assert code == 0, capsys.readouterr()
    assert GateLedger(tmp_path / "gate.jsonl").events()[0]["kind"] == "upgrade"
