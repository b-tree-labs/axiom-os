# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axiom.policy.agent_action_guard` (issues #218 + #219).

Policy middleware that composes the safety guards around any autonomous
destructive agent action. Replaces the per-action ad-hoc env-var checks
in `pr_check_auto_closer` with a single declarative shape + composer.

Today's guards (composable, first refusal short-circuits):

  1. Hard disable (env var)              — RIVET_GITHUB_ISSUE_CLOSE_DISABLE=1
  2. Legacy-alias hard disable           — RIVET_AUTO_CLOSE=0 (back-compat)
  3. Sentinel-file pause                 — <state_dir>/agents/<agent>/pause.<scope>.json
  4. State preconditions (caller probes) — e.g. main_currently_passing
  5. Volume bound (max per invocation)   — RIVET_GITHUB_ISSUE_CLOSE_MAX_PER_TICK
  6. Dry-run                             — RIVET_GITHUB_ISSUE_CLOSE_DRY_RUN=1
  7. Per-candidate action via do_one()
"""

from __future__ import annotations

import json
from pathlib import Path


def _action(*, candidates=None, agent="rivet", op_class="github.issue.close",
            name="auto_close_on_recovery"):
    from axiom.policy.agent_action_guard import AgentAction
    return AgentAction(
        agent=agent, op_class=op_class, name=name,
        candidates=list(candidates) if candidates is not None else [],
    )


def _identity_action(c):
    """Default do_one for tests that don't care about side effects."""
    return True


# ---------------------------------------------------------------------------
# Hard disable (env var)
# ---------------------------------------------------------------------------


class TestHardDisable:
    def test_disable_env_var_short_circuits(self, tmp_path, monkeypatch):
        from axiom.policy.agent_action_guard import guarded_act
        monkeypatch.setenv("RIVET_GITHUB_ISSUE_CLOSE_DISABLE", "1")
        calls: list = []
        decision = guarded_act(
            _action(candidates=[1, 2, 3]),
            do_one=lambda c: (calls.append(c), True)[1],
            state_dir=tmp_path,
        )
        assert decision.proceed is False
        assert decision.reason.startswith("hard_disable")
        assert calls == []

    def test_legacy_alias_honored(self, tmp_path, monkeypatch):
        """Existing operator-facing env var (RIVET_AUTO_CLOSE=0) keeps
        working. Aliases declared by the consumer, evaluated by the
        framework."""
        from axiom.policy.agent_action_guard import guarded_act
        monkeypatch.setenv("RIVET_AUTO_CLOSE", "0")
        decision = guarded_act(
            _action(candidates=[1, 2]),
            do_one=_identity_action,
            state_dir=tmp_path,
            env_aliases={"RIVET_AUTO_CLOSE=0": "disable"},
        )
        assert decision.proceed is False
        assert "hard_disable" in decision.reason


# ---------------------------------------------------------------------------
# Sentinel-file pause (#219)
# ---------------------------------------------------------------------------


def _write_sentinel(state_dir: Path, agent: str, scope: str,
                    reason: str = "operator-triggered"):
    p = state_dir / "agents" / agent / f"pause.{scope}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "paused_at": "2026-05-22T19:00:00Z",
        "paused_by": "test",
        "scope": scope,
        "reason": reason,
    }))
    return p


class TestSentinelPause:
    def test_pause_all_refuses_any_action(self, tmp_path):
        from axiom.policy.agent_action_guard import guarded_act
        _write_sentinel(tmp_path, "rivet", "all")
        decision = guarded_act(
            _action(candidates=[1]),
            do_one=_identity_action,
            state_dir=tmp_path,
        )
        assert decision.proceed is False
        assert "paused" in decision.reason

    def test_pause_specific_op_class_refuses_matching_action(self, tmp_path):
        from axiom.policy.agent_action_guard import guarded_act
        # Sentinel name uses the dotted op_class verbatim (file-system-safe enough)
        _write_sentinel(tmp_path, "rivet", "github.issue.close")
        decision = guarded_act(
            _action(candidates=[1, 2]),
            do_one=_identity_action,
            state_dir=tmp_path,
        )
        assert decision.proceed is False
        assert "paused" in decision.reason

    def test_pause_unrelated_scope_does_not_refuse(self, tmp_path):
        from axiom.policy.agent_action_guard import guarded_act
        _write_sentinel(tmp_path, "rivet", "github.branch.delete")
        decision = guarded_act(
            _action(candidates=[1]),
            do_one=_identity_action,
            state_dir=tmp_path,
        )
        assert decision.proceed is True

    def test_pause_for_different_agent_does_not_refuse(self, tmp_path):
        from axiom.policy.agent_action_guard import guarded_act
        _write_sentinel(tmp_path, "scan", "all")
        decision = guarded_act(
            _action(candidates=[1]),
            do_one=_identity_action,
            state_dir=tmp_path,
        )
        assert decision.proceed is True


# ---------------------------------------------------------------------------
# State preconditions
# ---------------------------------------------------------------------------


class TestStatePreconditions:
    def test_probe_pass_proceeds(self, tmp_path):
        from axiom.policy.agent_action_guard import guarded_act
        decision = guarded_act(
            _action(candidates=[1]),
            do_one=_identity_action,
            state_dir=tmp_path,
            state_probes=[lambda: (True, "")],
        )
        assert decision.proceed is True

    def test_probe_fail_refuses_with_reason(self, tmp_path):
        from axiom.policy.agent_action_guard import guarded_act
        decision = guarded_act(
            _action(candidates=[1]),
            do_one=_identity_action,
            state_dir=tmp_path,
            state_probes=[lambda: (False, "main is failing")],
        )
        assert decision.proceed is False
        assert "main is failing" in decision.reason

    def test_first_failing_probe_short_circuits(self, tmp_path):
        from axiom.policy.agent_action_guard import guarded_act
        second_called = []
        decision = guarded_act(
            _action(candidates=[1]),
            do_one=_identity_action,
            state_dir=tmp_path,
            state_probes=[
                lambda: (False, "first fail"),
                lambda: (second_called.append(True), (True, ""))[1],
            ],
        )
        assert decision.proceed is False
        assert "first fail" in decision.reason
        assert second_called == []  # never reached


# ---------------------------------------------------------------------------
# Volume bound (#218)
# ---------------------------------------------------------------------------


class TestVolumeBound:
    def test_candidates_at_limit_proceed(self, tmp_path, monkeypatch):
        from axiom.policy.agent_action_guard import guarded_act
        monkeypatch.setenv("RIVET_GITHUB_ISSUE_CLOSE_MAX_PER_TICK", "10")
        decision = guarded_act(
            _action(candidates=list(range(10))),
            do_one=_identity_action,
            state_dir=tmp_path,
        )
        assert decision.proceed is True

    def test_candidates_exceeding_limit_refuse_entire_batch(
        self, tmp_path, monkeypatch,
    ):
        """Refuse the WHOLE batch — partial-close is worse than no close
        (hard to tell what was swept; audit-trail muddied)."""
        from axiom.policy.agent_action_guard import guarded_act
        monkeypatch.setenv("RIVET_GITHUB_ISSUE_CLOSE_MAX_PER_TICK", "10")
        calls: list = []
        decision = guarded_act(
            _action(candidates=list(range(15))),
            do_one=lambda c: (calls.append(c), True)[1],
            state_dir=tmp_path,
        )
        assert decision.proceed is False
        assert "volume_limit" in decision.reason
        assert len(decision.refused) == 15
        assert calls == []  # no partial action

    def test_default_limit_when_env_unset(self, tmp_path, monkeypatch):
        """Default cap is the framework constant. Above default → refused."""
        from axiom.policy.agent_action_guard import (
            AGENT_ACTION_DEFAULT_MAX_PER_TICK,
            guarded_act,
        )
        # Make sure env is not set
        monkeypatch.delenv("RIVET_GITHUB_ISSUE_CLOSE_MAX_PER_TICK",
                           raising=False)
        decision = guarded_act(
            _action(candidates=list(range(AGENT_ACTION_DEFAULT_MAX_PER_TICK + 1))),
            do_one=_identity_action,
            state_dir=tmp_path,
        )
        assert decision.proceed is False
        assert "volume_limit" in decision.reason

    def test_notify_refusal_called_on_volume_block(
        self, tmp_path, monkeypatch,
    ):
        from axiom.policy.agent_action_guard import guarded_act
        monkeypatch.setenv("RIVET_GITHUB_ISSUE_CLOSE_MAX_PER_TICK", "2")
        notified: list = []
        guarded_act(
            _action(candidates=[1, 2, 3, 4, 5]),
            do_one=_identity_action,
            state_dir=tmp_path,
            notify_refusal=lambda subject, body: notified.append((subject, body)),
        )
        assert len(notified) == 1
        subject, body = notified[0]
        assert "5" in subject or "5" in body  # references the candidate count
        assert "2" in subject or "2" in body  # and the limit


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_env_proceeds_without_calling_do_one(
        self, tmp_path, monkeypatch,
    ):
        from axiom.policy.agent_action_guard import guarded_act
        monkeypatch.setenv("RIVET_GITHUB_ISSUE_CLOSE_DRY_RUN", "1")
        calls: list = []
        decision = guarded_act(
            _action(candidates=[1, 2, 3]),
            do_one=lambda c: (calls.append(c), True)[1],
            state_dir=tmp_path,
        )
        assert decision.proceed is True
        assert decision.reason == "dry_run"
        assert decision.completed == []
        assert decision.would_proceed == [1, 2, 3]
        assert calls == []

    def test_legacy_dry_run_alias_honored(self, tmp_path, monkeypatch):
        from axiom.policy.agent_action_guard import guarded_act
        monkeypatch.setenv("RIVET_AUTO_CLOSE_DRY_RUN", "1")
        calls: list = []
        decision = guarded_act(
            _action(candidates=[1]),
            do_one=lambda c: (calls.append(c), True)[1],
            state_dir=tmp_path,
            env_aliases={"RIVET_AUTO_CLOSE_DRY_RUN=1": "dry_run"},
        )
        assert decision.reason == "dry_run"
        assert calls == []

    def test_explicit_dry_run_kwarg_overrides_env(self, tmp_path, monkeypatch):
        """CLI --dry-run flag threads through as an explicit kwarg
        rather than mutating env vars (no side-effects for subsequent
        non-dry-run calls in the same process)."""
        from axiom.policy.agent_action_guard import guarded_act
        # Env not set; explicit kwarg drives the decision.
        monkeypatch.delenv("RIVET_GITHUB_ISSUE_CLOSE_DRY_RUN", raising=False)
        calls: list = []
        decision = guarded_act(
            _action(candidates=[1, 2]),
            do_one=lambda c: (calls.append(c), True)[1],
            state_dir=tmp_path,
            dry_run=True,
        )
        assert decision.reason == "dry_run"
        assert decision.would_proceed == [1, 2]
        assert calls == []


# ---------------------------------------------------------------------------
# Happy path + completed/refused composition
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_all_pass_calls_do_one_for_each(self, tmp_path):
        from axiom.policy.agent_action_guard import guarded_act
        calls: list = []
        decision = guarded_act(
            _action(candidates=[1, 2, 3]),
            do_one=lambda c: (calls.append(c), True)[1],
            state_dir=tmp_path,
        )
        assert decision.proceed is True
        assert calls == [1, 2, 3]
        assert decision.completed == [1, 2, 3]

    def test_per_candidate_failure_tracked(self, tmp_path):
        """A do_one() returning False (or raising) → that candidate goes
        to refused[], others continue. Different from volume-refused
        (which refuses the whole batch up front)."""
        from axiom.policy.agent_action_guard import guarded_act

        def do_one(c):
            if c == 2:
                return False
            return True

        decision = guarded_act(
            _action(candidates=[1, 2, 3]),
            do_one=do_one,
            state_dir=tmp_path,
        )
        assert decision.proceed is True
        assert decision.completed == [1, 3]
        assert decision.refused == [2]


# ---------------------------------------------------------------------------
# Pause / resume helpers
# ---------------------------------------------------------------------------


class TestPauseHelpers:
    def test_pause_action_writes_sentinel(self, tmp_path):
        from axiom.policy.agent_action_guard import pause_action
        path = pause_action(
            state_dir=tmp_path, agent="rivet", scope="github.issue.close",
            by="ben", reason="weird flap, investigating",
        )
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["scope"] == "github.issue.close"
        assert data["paused_by"] == "ben"
        assert "weird flap" in data["reason"]

    def test_resume_action_removes_sentinel(self, tmp_path):
        from axiom.policy.agent_action_guard import (
            pause_action, resume_action,
        )
        pause_action(state_dir=tmp_path, agent="rivet",
                     scope="github.issue.close", by="ben", reason="x")
        resume_action(state_dir=tmp_path, agent="rivet",
                      scope="github.issue.close")
        path = tmp_path / "agents" / "rivet" / "pause.github.issue.close.json"
        assert not path.exists()

    def test_resume_on_unpaused_is_noop(self, tmp_path):
        from axiom.policy.agent_action_guard import resume_action
        # No sentinel exists; resume should not raise
        resume_action(state_dir=tmp_path, agent="rivet", scope="all")

    def test_list_paused_returns_active_sentinels(self, tmp_path):
        from axiom.policy.agent_action_guard import (
            list_paused, pause_action,
        )
        pause_action(state_dir=tmp_path, agent="rivet",
                     scope="github.issue.close", by="ben", reason="a")
        pause_action(state_dir=tmp_path, agent="rivet",
                     scope="github.branch.delete", by="ben", reason="b")
        paused = list_paused(state_dir=tmp_path, agent="rivet")
        scopes = sorted(p["scope"] for p in paused)
        assert scopes == ["github.branch.delete", "github.issue.close"]


# ---------------------------------------------------------------------------
# D6.2 — Reversibility gate (ADR-045)
#
# guarded_act runs autonomous destructive actions. Per ADR-045 D6.2, an
# autonomous action must be reversible; an irreversible one never graduates
# past human approval and so must not flow through the autonomous guard.
# ---------------------------------------------------------------------------


def _action_irreversible(*, candidates=None, agent="tidy",
                         op_class="git.remote_ref.delete"):
    from axiom.policy.agent_action_guard import AgentAction
    return AgentAction(
        agent=agent, op_class=op_class, name="prune_remote_ref",
        candidates=list(candidates) if candidates is not None else [],
        reversible=False,
    )


class TestReversibilityGate:
    def test_irreversible_action_refused_before_acting(self, tmp_path):
        from axiom.policy.agent_action_guard import guarded_act
        calls: list = []
        decision = guarded_act(
            _action_irreversible(candidates=[1, 2]),
            do_one=lambda c: (calls.append(c), True)[1],
            state_dir=tmp_path,
        )
        assert decision.proceed is False
        assert "irreversible" in decision.reason
        assert calls == []          # never acted
        assert decision.refused == [1, 2]

    def test_reversible_default_proceeds(self, tmp_path):
        """Existing consumers construct AgentAction without setting
        reversible (defaults True) — behaviour is unchanged."""
        from axiom.policy.agent_action_guard import guarded_act
        decision = guarded_act(
            _action(candidates=[1, 2]),
            do_one=_identity_action,
            state_dir=tmp_path,
        )
        assert decision.proceed is True

    def test_irreversible_refused_even_in_dry_run(self, tmp_path):
        """The gate is a property of the action, not the run mode: an
        irreversible autonomous action is refused even under dry-run, so
        callers can't 'preview' a path the guard would never take."""
        from axiom.policy.agent_action_guard import guarded_act
        decision = guarded_act(
            _action_irreversible(candidates=[1]),
            do_one=_identity_action,
            state_dir=tmp_path,
            dry_run=True,
        )
        assert decision.proceed is False
        assert "irreversible" in decision.reason


# ---------------------------------------------------------------------------
# D6.3 — Volume confirm-downgrade (ADR-045)
#
# Default volume behaviour hard-refuses an over-limit batch. D6.3 adds an
# opt-in "confirm" mode: an over-limit batch is *downgraded to a prompt*
# (needs_confirmation) rather than refused outright, so a legitimate larger
# sweep can proceed after the operator confirms — without flattening the
# anomaly brake. A within-limit batch proceeds untouched.
# ---------------------------------------------------------------------------


class TestVolumeConfirmDowngrade:
    def test_over_limit_downgrades_to_confirmation_not_refusal(
        self, tmp_path, monkeypatch,
    ):
        from axiom.policy.agent_action_guard import guarded_act
        monkeypatch.setenv("TIDY_GIT_BRANCH_DELETE_MAX_PER_TICK", "10")
        calls: list = []
        decision = guarded_act(
            _action(candidates=list(range(21)), agent="tidy",
                    op_class="git.branch.delete", name="prune_merged"),
            do_one=lambda c: (calls.append(c), True)[1],
            state_dir=tmp_path,
            volume_mode="confirm",
        )
        assert decision.proceed is False
        assert decision.reason.startswith("needs_confirmation")
        assert "volume" in decision.reason
        assert decision.would_proceed == list(range(21))
        assert calls == []          # did not act; awaiting confirmation

    def test_within_limit_proceeds_under_confirm_mode(self, tmp_path, monkeypatch):
        from axiom.policy.agent_action_guard import guarded_act
        monkeypatch.setenv("TIDY_GIT_BRANCH_DELETE_MAX_PER_TICK", "10")
        calls: list = []
        decision = guarded_act(
            _action(candidates=[1, 2, 3], agent="tidy",
                    op_class="git.branch.delete", name="prune_merged"),
            do_one=lambda c: (calls.append(c), True)[1],
            state_dir=tmp_path,
            volume_mode="confirm",
        )
        assert decision.proceed is True
        assert decision.completed == [1, 2, 3]
        assert calls == [1, 2, 3]

    def test_volume_mode_off_bypasses_limit(self, tmp_path, monkeypatch):
        """After the operator confirms an over-limit batch, the consumer
        re-runs with volume_mode='off' to proceed — they've approved the
        batch size, so the volume gate is bypassed for that call."""
        from axiom.policy.agent_action_guard import guarded_act
        monkeypatch.setenv("TIDY_GIT_BRANCH_DELETE_MAX_PER_TICK", "2")
        calls: list = []
        decision = guarded_act(
            _action(candidates=list(range(5)), agent="tidy",
                    op_class="git.branch.delete", name="prune_merged"),
            do_one=lambda c: (calls.append(c), True)[1],
            state_dir=tmp_path,
            volume_mode="off",
        )
        assert decision.proceed is True
        assert len(decision.completed) == 5

    def test_default_mode_still_hard_refuses(self, tmp_path, monkeypatch):
        """Back-compat: without volume_mode, an over-limit batch is
        refused outright (the existing RIVET behaviour)."""
        from axiom.policy.agent_action_guard import guarded_act
        monkeypatch.setenv("TIDY_GIT_BRANCH_DELETE_MAX_PER_TICK", "10")
        decision = guarded_act(
            _action(candidates=list(range(21)), agent="tidy",
                    op_class="git.branch.delete", name="prune_merged"),
            do_one=_identity_action,
            state_dir=tmp_path,
        )
        assert decision.proceed is False
        assert "volume_limit" in decision.reason
        assert len(decision.refused) == 21
