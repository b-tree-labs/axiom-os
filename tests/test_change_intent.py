# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A change Axiom makes to a node is recorded BEFORE it acts (ADR-182 D5a).

Downtime is attributed to "ours" only when it overlaps a change Axiom recorded
itself making. If the record were written after the change, the change most
likely to cause an outage, the one that crashed half way, would leave no record,
and its outage would read as unattributed or, worse, as somebody else's.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from axiom.infra.change_intent import CHANGE_KINDS, changes, overlapping, record_change


@pytest.fixture
def ledger(tmp_path):
    return tmp_path / "changes.jsonl"


def test_the_intent_is_on_disk_before_the_change_runs(ledger):
    seen = []
    with record_change("update", "axiom 0.68.0", path=ledger):
        seen = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(seen) == 1
    assert seen[0]["phase"] == "intent"
    assert seen[0]["kind"] == "update"


def test_a_change_that_finishes_has_an_end_and_an_outcome(ledger):
    with record_change("migration", "receipts", path=ledger):
        pass
    (c,) = changes(path=ledger)
    assert c.kind == "migration" and c.subject == "receipts"
    assert c.outcome == "ok"
    assert c.ended is not None and c.ended >= c.started


def test_a_change_that_raises_is_recorded_failed_and_the_error_propagates(ledger):
    with pytest.raises(RuntimeError):
        with record_change("config", "llm.tier", path=ledger):
            raise RuntimeError("boom")
    (c,) = changes(path=ledger)
    assert c.outcome == "failed"
    assert "boom" in c.detail


def test_the_body_can_name_its_own_outcome(ledger):
    with record_change("update", "axiom 0.68.0", path=ledger) as change:
        change.outcome = "rolled_back"
    (c,) = changes(path=ledger)
    assert c.outcome == "rolled_back"


def test_a_change_that_never_finished_is_still_a_change(ledger):
    """The crash case: intent written, process killed before any outcome."""
    with record_change("deploy", "http", path=ledger):
        pass
    lines = ledger.read_text().splitlines()
    ledger.write_text(lines[0] + "\n")  # outcome line lost with the process
    (c,) = changes(path=ledger)
    assert c.ended is None
    assert c.outcome is None
    # An open change overlaps everything after it began: an outage that
    # follows a crashed change is ours until shown otherwise.
    later = c.started + timedelta(hours=3)
    assert overlapping(later, later + timedelta(minutes=1), path=ledger) == [c]


def test_a_torn_last_line_is_skipped_not_fatal(ledger):
    with record_change("config", "a", path=ledger):
        pass
    with ledger.open("a") as fh:
        fh.write('{"phase": "intent", "kin')
    assert [c.subject for c in changes(path=ledger)] == ["a"]


def test_the_kinds_are_closed(ledger):
    assert set(CHANGE_KINDS) == {"deploy", "update", "migration", "config"}
    with pytest.raises(ValueError):
        with record_change("reboot", "x", path=ledger):
            pass
    assert not ledger.exists() or ledger.read_text() == ""


def test_overlap_selects_only_changes_that_touch_the_interval(ledger):
    with record_change("deploy", "early", path=ledger):
        pass
    t0 = datetime.now(UTC)
    later = t0 + timedelta(hours=1)
    assert overlapping(later, later + timedelta(minutes=5), path=ledger) == []
    hit = overlapping(t0 - timedelta(minutes=5), t0 + timedelta(minutes=5), path=ledger)
    assert [c.subject for c in hit] == ["early"]


def test_the_default_ledger_lives_in_the_machine_state_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("AXI_CHANGE_LEDGER", raising=False)
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    with record_change("config", "x"):
        pass
    assert (tmp_path / "availability" / "changes.jsonl").exists()
    assert [c.subject for c in changes()] == ["x"]


def test_the_ledger_can_be_redirected(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere.jsonl"
    monkeypatch.setenv("AXI_CHANGE_LEDGER", str(target))
    with record_change("deploy", "x"):
        pass
    assert target.exists()
