# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Guard-emitted decision provenance (issue #665).

``guarded_act`` journals one record per candidate into the action
ledger — automatically, for every consumer, with zero consumer changes.
Refusals are first-class records carrying the refusing rule; the only
optional consumer surface is the ``undo_ref_for`` callback.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _jsonl_ledger_env(tmp_path, monkeypatch):
    """Pin emission to the JSONL backend under tmp — no live PG needed."""
    from axiom.policy import action_ledger
    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.delenv("AXIOM_AUDIT_HMAC_KEY", raising=False)
    action_ledger._reset_backend_cache()
    yield
    action_ledger._reset_backend_cache()


def _action(*, candidates=None, agent="rivet", op_class="github.issue.close",
            name="auto_close_on_recovery", reversible=True):
    from axiom.policy.agent_action_guard import AgentAction
    return AgentAction(
        agent=agent, op_class=op_class, name=name,
        candidates=list(candidates) if candidates is not None else [],
        reversible=reversible,
    )


def _rows(state_dir, **filters):
    from axiom.policy.action_ledger import ActionLedger
    return ActionLedger(state_dir=state_dir).query(**filters)


class TestSuccessAndFailure:

    def test_one_record_per_completed_candidate(self, tmp_path):
        from axiom.policy.agent_action_guard import guarded_act
        guarded_act(
            _action(candidates=["a", "b"]),
            do_one=lambda c: True,
            state_dir=tmp_path,
        )
        rows = _rows(tmp_path)
        assert len(rows) == 2
        assert {r["candidate"] for r in rows} == {"a", "b"}
        assert all(r["outcome"] == "proceeded" for r in rows)
        assert all(r["agent"] == "rivet" for r in rows)
        assert all(r["op_class"] == "github.issue.close" for r in rows)
        assert all(r["dry_run"] is False for r in rows)
        assert all(r["refusing_rule"] is None for r in rows)
        assert all("act" in r["guards"] for r in rows)

    def test_do_one_failure_recorded_as_failed(self, tmp_path):
        from axiom.policy.agent_action_guard import guarded_act
        guarded_act(
            _action(candidates=["good", "bad"]),
            do_one=lambda c: c == "good",
            state_dir=tmp_path,
        )
        by = {r["candidate"]: r for r in _rows(tmp_path)}
        assert by["good"]["outcome"] == "proceeded"
        assert by["bad"]["outcome"] == "failed"

    def test_undo_ref_callback_recorded(self, tmp_path):
        from axiom.policy.agent_action_guard import guarded_act
        guarded_act(
            _action(candidates=["feat/x"]),
            do_one=lambda c: True,
            state_dir=tmp_path,
            undo_ref_for=lambda c: f"refs/tidy-archive/local/{c}",
        )
        (row,) = _rows(tmp_path)
        assert row["undo_ref"] == "refs/tidy-archive/local/feat/x"


class TestRefusalsAreFirstClass:

    def test_hard_disable_emits_refusals_with_rule(self, tmp_path, monkeypatch):
        from axiom.policy.agent_action_guard import guarded_act
        monkeypatch.setenv("RIVET_GITHUB_ISSUE_CLOSE_DISABLE", "1")
        guarded_act(
            _action(candidates=["a", "b"]),
            do_one=lambda c: True,
            state_dir=tmp_path,
        )
        rows = _rows(tmp_path, outcome="refused")
        assert len(rows) == 2
        assert all(r["refusing_rule"] == "hard_disable" for r in rows)

    def test_pause_sentinel_refusal_records_scope(self, tmp_path):
        from axiom.policy.agent_action_guard import guarded_act, pause_action
        pause_action(
            state_dir=tmp_path, agent="rivet", scope="all",
            by="tester", reason="incident",
        )
        guarded_act(
            _action(candidates=["a"]),
            do_one=lambda c: True,
            state_dir=tmp_path,
        )
        (row,) = _rows(tmp_path)
        assert row["outcome"] == "refused"
        assert row["refusing_rule"] == "paused:all"

    def test_state_probe_refusal_records_reason(self, tmp_path):
        from axiom.policy.agent_action_guard import guarded_act
        guarded_act(
            _action(candidates=["a"]),
            do_one=lambda c: True,
            state_dir=tmp_path,
            state_probes=[lambda: (False, "main_red")],
        )
        (row,) = _rows(tmp_path)
        assert row["outcome"] == "refused"
        assert row["refusing_rule"] == "main_red"

    def test_volume_refusal_records_rule(self, tmp_path, monkeypatch):
        from axiom.policy.agent_action_guard import guarded_act
        monkeypatch.setenv("RIVET_GITHUB_ISSUE_CLOSE_MAX_PER_TICK", "1")
        guarded_act(
            _action(candidates=["a", "b"]),
            do_one=lambda c: True,
            state_dir=tmp_path,
        )
        rows = _rows(tmp_path, outcome="refused")
        assert len(rows) == 2
        assert all(r["refusing_rule"].startswith("volume_limit_exceeded") for r in rows)

    def test_irreversible_refusal_recorded(self, tmp_path):
        from axiom.policy.agent_action_guard import guarded_act
        guarded_act(
            _action(candidates=["a"], reversible=False),
            do_one=lambda c: True,
            state_dir=tmp_path,
        )
        (row,) = _rows(tmp_path)
        assert row["outcome"] == "refused"
        assert row["refusing_rule"].startswith("irreversible")

    def test_refusal_with_no_candidates_still_journals(self, tmp_path, monkeypatch):
        from axiom.policy.agent_action_guard import guarded_act
        monkeypatch.setenv("RIVET_GITHUB_ISSUE_CLOSE_DISABLE", "1")
        guarded_act(
            _action(candidates=[]),
            do_one=lambda c: True,
            state_dir=tmp_path,
        )
        (row,) = _rows(tmp_path)
        assert row["outcome"] == "refused"
        assert row["candidate"] == "(batch)"


class TestDryRun:

    def test_dry_run_records_flag_not_refusal(self, tmp_path):
        from axiom.policy.agent_action_guard import guarded_act
        guarded_act(
            _action(candidates=["a", "b"]),
            do_one=lambda c: True,
            state_dir=tmp_path,
            dry_run=True,
        )
        rows = _rows(tmp_path)
        assert len(rows) == 2
        assert all(r["dry_run"] is True for r in rows)
        assert all(r["outcome"] == "proceeded" for r in rows)


class TestEmissionNeverBreaksTheGuard:

    def test_ledger_failure_does_not_change_decision(self, tmp_path, monkeypatch):
        from axiom.policy import agent_action_guard as guard_mod

        def _boom(*a, **k):
            raise RuntimeError("ledger down")

        monkeypatch.setattr(
            "axiom.policy.action_ledger.record_guard_decision", _boom,
        )
        decision = guard_mod.guarded_act(
            _action(candidates=["a"]),
            do_one=lambda c: True,
            state_dir=tmp_path,
        )
        assert decision.proceed is True
        assert decision.completed == ["a"]
