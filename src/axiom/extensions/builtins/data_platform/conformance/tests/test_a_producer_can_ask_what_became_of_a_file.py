# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A producer asking "did my upload arrive?" means "can I see it?".

A partner's files reached bronze and were told "landed", while every one of them
sat unread and their newest reading stayed three weeks old. Bronze is not the
answer to that question, so the conform pass records what became of each file,
and a decline stops being final when the logic that declined it changes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from axiom.extensions.builtins.data_platform.conformance import deposits as dp
from axiom.extensions.builtins.data_platform.conformance.deposits import (
    DepositDeclined,
    DepositRegistry,
    PgDepositLedger,
    conform_deposits,
)
from axiom.extensions.builtins.data_platform.skills import friendly

REF = "loop/archive-v1"
SITE = "example-loop"
CONNECTOR = "loop-share"


def _write(root: Path, name: str, body: bytes) -> None:
    import hashlib

    sha = hashlib.sha256(body).hexdigest()
    content = root / "_content" / sha[:2] / sha
    content.parent.mkdir(parents=True, exist_ok=True)
    content.write_bytes(body)
    day = root / CONNECTOR / "_records" / "2026-11-03"
    day.mkdir(parents=True, exist_ok=True)
    (day / f"{name}.json").write_text(
        json.dumps(
            {
                "item_id": name,
                "display_name": name,
                "source_path": f"/share/{name}",
                "content_sha256": sha,
                "fetched_at": "2026-11-03T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


class _Ledger:
    """The ledger's rules over a dict, so they run without a database."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    def done(self, sha, ref, version=None, **facts):
        row = self.rows.get((sha, ref))
        if row is None or row["outcome"] == dp.FAILED:
            return False
        return bool(row["rows_out"]) or version is None or row["version"] == version

    def record(self, sha, ref, connector, rows_out, **facts):
        self.rows[(sha, ref)] = {"rows_out": rows_out, **facts}

    def fail(self, sha, ref, connector, reason, **facts):
        self.rows[(sha, ref)] = {"rows_out": 0, "outcome": dp.FAILED, "reason": reason, **facts}


def _reader(deposit):
    text = deposit.path.read_bytes()
    if deposit.name.startswith("notes"):
        raise DepositDeclined("a run note, not readings")
    if deposit.name.startswith("empty"):
        return
    if deposit.name.startswith("bad"):
        raise ValueError("line 3 is not a number")
    for i, channel in enumerate(("T1", "T2")):
        yield {
            "feed": "loop.archive",
            "channel": channel,
            "ts": datetime(2026, 11, 3, 0, i, tzinfo=UTC),
            "value": float(len(text)),
        }


def _conform(root, ledger, fn=_reader):
    registry = DepositRegistry()
    registry.register(REF, fn)
    return conform_deposits(
        root, registry, {CONNECTOR: REF}, {CONNECTOR: SITE}, upsert=lambda r: None, ledger=ledger
    )


def _by_name(ledger):
    return {row["name"]: row for row in ledger.rows.values()}


def test_each_file_says_what_became_of_it(tmp_path):
    _write(tmp_path, "day.csv", b"a")
    _write(tmp_path, "notes.txt", b"b")
    _write(tmp_path, "empty.csv", b"c")
    _write(tmp_path, "bad.csv", b"d")
    ledger = _Ledger()
    _conform(tmp_path, ledger)
    found = _by_name(ledger)

    day = found["day.csv"]
    assert (day["outcome"], day["rows_out"], day["channels"]) == (dp.LANDED, 2, 2)
    assert day["site"] == SITE
    assert day["first_ts"] == datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
    assert day["last_ts"] == datetime(2026, 11, 3, 0, 1, tzinfo=UTC)

    assert (found["notes.txt"]["outcome"], found["notes.txt"]["reason"]) == (
        dp.DECLINED,
        "a run note, not readings",
    )
    assert (found["empty.csv"]["outcome"], found["empty.csv"]["reason"]) == (
        dp.DECLINED,
        dp.NOT_THIS_FORMAT,
    ), "a zero is never unexplained"
    assert found["bad.csv"]["outcome"] == dp.FAILED
    assert "line 3" in found["bad.csv"]["reason"]


def test_a_failed_file_is_told_and_still_retried(tmp_path):
    _write(tmp_path, "bad.csv", b"d")
    ledger = _Ledger()
    assert _conform(tmp_path, ledger)["errored"] == 1
    assert _conform(tmp_path, ledger)["errored"] == 1


def test_a_decline_is_only_as_final_as_the_logic_that_made_it(tmp_path):
    """The stuck file: declined by yesterday's reader, readable by today's."""
    _write(tmp_path, "notes.txt", b"b")
    ledger = _Ledger()

    def old(deposit):
        return _reader(deposit)

    old.conform_version = "reader@1"
    assert _conform(tmp_path, ledger, old)["declined"]
    assert _conform(tmp_path, ledger, old)["deposits_unchanged"] == 1

    def new(deposit):
        yield {"feed": "f", "channel": "T1", "ts": datetime(2026, 11, 3, tzinfo=UTC), "value": 1.0}

    new.conform_version = "reader@2"
    assert _conform(tmp_path, ledger, new)["rows_out"] == 1
    assert _by_name(ledger)["notes.txt"]["outcome"] == dp.LANDED


def test_landed_bytes_are_not_reread_when_the_logic_changes(tmp_path):
    """A rebuild is how landed readings are re-derived; a new version alone must not."""
    _write(tmp_path, "day.csv", b"a")
    ledger = _Ledger()

    def first(deposit):
        return _reader(deposit)

    first.conform_version = "reader@1"
    _conform(tmp_path, ledger, first)

    def second(deposit):
        return _reader(deposit)

    second.conform_version = "reader@2"
    assert _conform(tmp_path, ledger, second)["deposits_unchanged"] == 1


class _Cursor:
    def __init__(self, answers):
        self.answers = list(answers)
        self.sql: list[str] = []

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def fetchone(self):
        return self.answers.pop(0)

    def fetchall(self):
        return [self.answers.pop(0)]


@pytest.mark.parametrize(
    ("row", "version", "done"),
    [
        (None, "v2", False),
        (("landed", 5, "v1", "s"), "v2", True),
        (("declined", 0, "v2", "s"), "v2", True),
        (("declined", 0, "v1", "s"), "v2", False),
        ((None, 0, None, None), "v2", False),  # declined before outcomes were kept: read once more
        ((None, 5, None, None), "v2", True),
        (("failed", 0, "v2", "s"), "v2", False),
    ],
)
def test_the_real_ledger_decides_the_same_way(row, version, done):
    columns = len(PgDepositLedger.OUTCOME_COLUMNS)
    cur = _Cursor([(columns,), ("gold.deposit_status",), row])
    ledger = PgDepositLedger(cur)
    assert not any("ALTER" in s or "VIEW" in s for s in cur.sql), "a settled shape takes no lock"
    assert ledger.done("sha", REF, version) is done


def test_a_file_that_landed_before_outcomes_gets_its_name():
    columns = len(PgDepositLedger.OUTCOME_COLUMNS)
    cur = _Cursor([(columns,), ("gold.deposit_status",), (None, 5, None, None)])
    ledger = PgDepositLedger(cur)
    assert ledger.done("sha", REF, "v2", site=SITE, name="old.csv") is True
    assert any(s.startswith("UPDATE silver.conformed_deposits") for s in cur.sql)


def test_an_existing_ledger_gains_the_outcome_columns_and_the_gold_view():
    cur = _Cursor([(0,), (None,)])
    PgDepositLedger(cur)
    assert sum("ALTER TABLE" in s for s in cur.sql) == len(PgDepositLedger.OUTCOME_COLUMNS)
    assert any("VIEW gold.deposit_status" in s for s in cur.sql)


# --- the verb every surface reads ----------------------------------------------

ROWS = [
    {
        "file_name": "nightly-2.bz2",
        "outcome": "landed",
        "reason": None,
        "readings": 3420201,
        "channels": 102,
        "first_ts": "2026-11-02T10:00:02+00:00",
        "last_ts": "2026-11-03T09:59:41+00:00",
        "value": "2026-11-06T22:00:00+00:00",
    },
    {
        "file_name": "notes.txt",
        "outcome": "declined",
        "reason": "a run note, not readings",
        "readings": 0,
        "channels": None,
        "first_ts": None,
        "last_ts": None,
        "value": "2026-11-06T21:00:00+00:00",
    },
]


@pytest.fixture
def served(monkeypatch):
    calls = []

    def fake(ctx, **kw):
        calls.append(kw)
        return [dict(r) for r in ROWS]

    monkeypatch.setattr(friendly, "_agg", fake)
    return calls


def test_the_verb_answers_with_outcomes_and_the_newest_reading(served):
    res = friendly.deposits({"site": SITE}, None)
    assert res.ok
    data = res.value["data"]
    assert data["summary"] == {"landed": 1, "declined": 1, "failed": 0}
    assert data["newest_reading"] == "2026-11-03T09:59:41+00:00"
    assert [f["file"] for f in data["files"]] == ["nightly-2.bz2", "notes.txt"]
    assert res.value["provenance"]["source"] == "gold.deposit_status"
    assert served[0]["table"] == "deposit_status", "consumers read gold"
    assert f"site = {friendly._lit(SITE)}" == served[0]["filter"]


def test_the_verb_can_show_only_what_went_wrong(served):
    data = friendly.deposits({"site": SITE, "outcome": "declined"}, None).value["data"]
    assert [f["file"] for f in data["files"]] == ["notes.txt"]
    assert data["summary"]["landed"] == 1, "the summary stays whole"


def test_a_mistyped_outcome_says_what_is_allowed(served):
    res = friendly.deposits({"site": SITE, "outcome": "lost"}, None)
    assert not res.ok and "landed" in res.errors[0]


def test_a_node_with_no_status_yet_says_so(monkeypatch):
    def refuse(ctx, **kw):
        raise friendly._Refused("unknown table deposit_status")

    monkeypatch.setattr(friendly, "_agg", refuse)
    res = friendly.deposits({"site": SITE}, None)
    assert not res.ok and "conform pass" in res.errors[0]
