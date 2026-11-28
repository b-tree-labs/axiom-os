# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The producer must not invent a cadence, and must not pick a stale source.

Both failures are silent and both are worse than reporting nothing: the
consumer turns a declared cadence into a count of missed reports, so a
fabricated one manufactures a finding, and a stale timestamp reports a live
agent as dark.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from axiom.extensions.builtins.agents import liveness


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """An agents state directory with no extensions declared behind it."""
    monkeypatch.setattr(liveness, "_declared_cadences", lambda: {})
    monkeypatch.setattr(liveness, "_agent_principals", lambda: {})
    # Isolate from the real consent file. Without this the roster tests read
    # the developer's own `~/.axi/agents_consent.json` and their assertions
    # change with it — which is how this suite started failing on a machine
    # where one agent happened to be enabled.
    monkeypatch.setattr(liveness, "_unconsented", lambda: None)
    monkeypatch.setenv("AXIOM_SITE", "testsite")
    return tmp_path


def _write_state(directory, mapping: dict[str, float]) -> None:
    state = directory / ".background-service"
    state.mkdir(parents=True, exist_ok=True)
    (state / "state.json").write_text(json.dumps(mapping))


def _write_ledger(directory, name: str, *stamps: str) -> None:
    agent = directory / name
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "heartbeat.jsonl").write_text(
        "".join(json.dumps({"agent": name, "ts": s}) + "\n" for s in stamps)
    )


class TestCadenceIsNeverInvented:
    def test_an_undeclared_cadence_is_absent_not_the_parser_default(
        self, agents_dir, monkeypatch
    ):
        # AgentDef.heartbeat_interval defaults to 300, so the PARSED object
        # always carries a number. Reporting it would tell the consumer that
        # an agent declaring nothing had declared five minutes, and the
        # consumer counts missed reports from exactly that.
        _write_ledger(agents_dir, "burn-e", "2026-05-04T08:31:05+00:00")
        [row] = liveness.agent_liveness(agents_dir=agents_dir)
        assert row["agent"] == "@burn-e:testsite"
        assert "cadenceSeconds" not in row

    def test_a_declared_cadence_is_reported(self, agents_dir, monkeypatch):
        monkeypatch.setattr(liveness, "_declared_cadences", lambda: {"hygiene": 300})
        [row] = liveness.agent_liveness(agents_dir=agents_dir)
        assert row["cadenceSeconds"] == 300


class TestTheLatestEvidenceWins:
    def test_an_agent_fired_outside_the_dispatcher_is_not_reported_as_stale(
        self, agents_dir, monkeypatch
    ):
        # The live case on this machine: the dispatcher last fired `release`
        # weeks ago and RIVET's own ledger shows it wrote hours ago, because
        # something else is firing it. Trusting the dispatcher alone reports a
        # demonstrably running agent as dark.
        monkeypatch.setattr(liveness, "_declared_cadences", lambda: {"release": 300})
        monkeypatch.setattr(liveness, "_agent_principals", lambda: {"release": "rivet"})
        old = datetime(2026, 9, 7, 14, 37, tzinfo=UTC)
        _write_state(agents_dir, {"release": old.timestamp()})
        _write_ledger(agents_dir, "rivet", "2026-09-29T07:15:26+00:00")

        [row] = liveness.agent_liveness(agents_dir=agents_dir)
        assert row["agent"] == "@rivet:testsite"
        assert row["lastReportAt"].startswith("2026-09-29")

    def test_an_agent_with_no_ledger_still_gets_the_dispatcher_time(self, agents_dir, monkeypatch):
        # The mirror case. Most agents keep no ledger of their own — a ledger
        # is one agent's convention, not a platform guarantee — so reading
        # only ledgers loses them entirely.
        monkeypatch.setattr(liveness, "_declared_cadences", lambda: {"vault": 3600})
        monkeypatch.setattr(liveness, "_agent_principals", lambda: {"vault": "keep"})
        when = datetime(2026, 9, 21, 22, 48, 33, tzinfo=UTC)
        _write_state(agents_dir, {"vault": when.timestamp()})

        [row] = liveness.agent_liveness(agents_dir=agents_dir)
        assert row["agent"] == "@keep:testsite"
        assert row["lastReportAt"].startswith("2026-09-21")


class TestTheRosterLosesNobody:
    def test_a_declared_agent_that_never_ran_is_reported_not_dropped(
        self, agents_dir, monkeypatch
    ):
        # "No report has ever arrived" is the finding. Dropping the row hides
        # it behind a shorter list, which reads as a smaller fleet rather than
        # a broken one.
        monkeypatch.setattr(liveness, "_declared_cadences", lambda: {"signals": 30})
        monkeypatch.setattr(liveness, "_agent_principals", lambda: {"signals": "scan"})
        [row] = liveness.agent_liveness(agents_dir=agents_dir)
        assert row == {"agent": "@scan:testsite", "lastReportAt": None, "cadenceSeconds": 30}

    def test_an_extension_and_its_ledger_are_one_agent_not_two(self, agents_dir, monkeypatch):
        # The principal is derived from [sender].display_name, so `release`
        # and `rivet` name the same agent. Keying on the wrong one of the two
        # produces a duplicate row and halves every liveness reading.
        monkeypatch.setattr(liveness, "_declared_cadences", lambda: {"release": 300})
        monkeypatch.setattr(liveness, "_agent_principals", lambda: {"release": "rivet"})
        _write_ledger(agents_dir, "rivet", "2026-09-29T07:15:26+00:00")
        rows = liveness.agent_liveness(agents_dir=agents_dir)
        assert len(rows) == 1


class TestItSurvivesBadInput:
    def test_a_truncated_final_write_does_not_hide_the_record_above_it(self, agents_dir):
        agent = agents_dir / "rivet"
        agent.mkdir(parents=True)
        (agent / "heartbeat.jsonl").write_text(
            json.dumps({"agent": "rivet", "ts": "2026-09-29T07:15:26+00:00"}) + "\n{\"agent\": \"riv"
        )
        [row] = liveness.agent_liveness(agents_dir=agents_dir)
        assert row["lastReportAt"].startswith("2026-09-29")

    @pytest.mark.parametrize(
        "payload",
        ["", "not json at all\n", json.dumps([1, 2, 3]), json.dumps({"agent": "x"})],
    )
    def test_an_unusable_ledger_reports_no_time_rather_than_raising(self, agents_dir, payload):
        agent = agents_dir / "rivet"
        agent.mkdir(parents=True)
        (agent / "heartbeat.jsonl").write_text(payload)
        rows = liveness.agent_liveness(agents_dir=agents_dir)
        assert rows == [] or rows[0]["lastReportAt"] is None

    def test_a_missing_directory_is_an_empty_roster_not_an_exception(self, tmp_path, monkeypatch):
        monkeypatch.setattr(liveness, "_declared_cadences", lambda: {})
        monkeypatch.setattr(liveness, "_agent_principals", lambda: {})
        assert liveness.agent_liveness(agents_dir=tmp_path / "nope") == []

    def test_a_naive_stamp_is_read_as_utc_rather_than_dropped(self, agents_dir):
        _write_ledger(agents_dir, "rivet", "2026-09-29T07:15:26")
        [row] = liveness.agent_liveness(agents_dir=agents_dir)
        assert row["lastReportAt"] is not None
        assert row["lastReportAt"].endswith("+00:00")


class TestChosenSilence:
    """An agent nobody enabled is not dark, and saying so was a real defect."""

    def test_an_unconsented_agent_is_marked_stopped_by_the_operator(
        self, agents_dir, monkeypatch
    ):
        # The failure this exists to prevent: five agents that had never been
        # enabled were reported DARK on this surface's first day, because the
        # producer never read the consent record.
        monkeypatch.setattr(liveness, "_declared_cadences", lambda: {"release": 300})
        monkeypatch.setattr(liveness, "_agent_principals", lambda: {"release": "rivet"})
        monkeypatch.setattr(liveness, "_unconsented", lambda: {"hygiene"})
        [row] = liveness.agent_liveness(agents_dir=agents_dir)
        assert row["stoppedByOperator"] is True

    def test_a_consented_agent_is_not_marked(self, agents_dir, monkeypatch):
        monkeypatch.setattr(liveness, "_declared_cadences", lambda: {"hygiene": 300})
        monkeypatch.setattr(liveness, "_agent_principals", lambda: {"hygiene": "tidy"})
        monkeypatch.setattr(liveness, "_unconsented", lambda: {"hygiene"})
        [row] = liveness.agent_liveness(agents_dir=agents_dir)
        assert "stoppedByOperator" not in row

    def test_NO_RECORDED_DECISION_MARKS_NOBODY(self, agents_dir, monkeypatch):
        # The distinction that matters. `None` means nothing was decided, so
        # dispatch is unrestricted and no agent's silence was chosen. Marking
        # everything stopped here would invent an intent nobody expressed and
        # silence every real finding at once.
        monkeypatch.setattr(liveness, "_declared_cadences", lambda: {"release": 300})
        monkeypatch.setattr(liveness, "_agent_principals", lambda: {"release": "rivet"})
        monkeypatch.setattr(liveness, "_unconsented", lambda: None)
        [row] = liveness.agent_liveness(agents_dir=agents_dir)
        assert "stoppedByOperator" not in row

    def test_opting_out_entirely_marks_every_agent(self, agents_dir, monkeypatch):
        monkeypatch.setattr(liveness, "_declared_cadences", lambda: {"release": 300, "vault": 60})
        monkeypatch.setattr(liveness, "_agent_principals", lambda: {})
        monkeypatch.setattr(liveness, "_unconsented", lambda: set())
        rows = liveness.agent_liveness(agents_dir=agents_dir)
        assert len(rows) == 2
        assert all(r["stoppedByOperator"] for r in rows)
