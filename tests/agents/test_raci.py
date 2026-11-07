# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for the RACI escalation state machine.

Per `feedback_raci_automation_escalation`: RACI agents default to
propose → ask → schedule|back-off|off. Yes → APPROVED (action runs on
schedule). No → BACKING_OFF; cooldown grows (1d → 3d → 9d). After the
third rejection → DISABLED (do not re-ask). Pre-approval at install
time skips propose entirely.
"""

from __future__ import annotations


class TestProposalLifecycle:
    def test_first_time_returns_ask(self):
        from axiom.agents.raci import ProposalDecision, RACILedger

        ledger = RACILedger(now=lambda: 0.0)
        assert ledger.propose("local-rag-steward") is ProposalDecision.ASK

    def test_yes_transitions_to_auto(self):
        from axiom.agents.raci import ProposalDecision, RACILedger

        ledger = RACILedger(now=lambda: 0.0)
        ledger.record_yes("local-rag-steward")
        assert ledger.propose("local-rag-steward") is ProposalDecision.AUTO

    def test_no_transitions_to_skip_during_cooldown(self):
        from axiom.agents.raci import ProposalDecision, RACILedger

        ledger = RACILedger(now=lambda: 0.0)
        ledger.record_no("local-rag-steward")
        assert ledger.propose("local-rag-steward") is ProposalDecision.SKIP

    def test_cooldown_elapses_to_ask_again(self):
        from axiom.agents.raci import ProposalDecision, RACILedger

        clock = {"t": 0.0}
        ledger = RACILedger(now=lambda: clock["t"])
        ledger.record_no("local-rag-steward")
        # Default first-no cooldown is 1 day = 86400 s
        clock["t"] = 86401.0
        assert ledger.propose("local-rag-steward") is ProposalDecision.ASK

    def test_three_nos_disable_permanently(self):
        from axiom.agents.raci import ProposalDecision, RACILedger

        clock = {"t": 0.0}
        ledger = RACILedger(now=lambda: clock["t"])
        for cooldown in (0.0, 86400.0 * 1.5, 86400.0 * 6):
            clock["t"] = cooldown
            ledger.record_no("local-rag-steward")
        clock["t"] = 86400.0 * 365  # a year later
        assert ledger.propose("local-rag-steward") is ProposalDecision.SKIP
        assert ledger.is_disabled("local-rag-steward")

    def test_pre_approval_skips_proposal(self):
        from axiom.agents.raci import ProposalDecision, RACILedger

        ledger = RACILedger(now=lambda: 0.0)
        ledger.pre_approve("local-rag-steward")
        assert ledger.propose("local-rag-steward") is ProposalDecision.AUTO

    def test_pre_approval_overrides_prior_no(self):
        from axiom.agents.raci import ProposalDecision, RACILedger

        ledger = RACILedger(now=lambda: 0.0)
        ledger.record_no("local-rag-steward")
        ledger.record_no("local-rag-steward")
        ledger.pre_approve("local-rag-steward")
        assert ledger.propose("local-rag-steward") is ProposalDecision.AUTO


class TestExponentialBackoff:
    def test_backoff_doubles_each_no(self):
        from axiom.agents.raci import RACILedger

        clock = {"t": 0.0}
        ledger = RACILedger(now=lambda: clock["t"])

        ledger.record_no("ci-diagnose")
        first_next = ledger.next_ask_at("ci-diagnose")
        assert first_next == 86400.0  # 1 day

        clock["t"] = first_next
        ledger.record_no("ci-diagnose")
        second_next = ledger.next_ask_at("ci-diagnose")
        assert second_next == clock["t"] + 86400.0 * 3  # 3 days from t1


class TestActionClassIsolation:
    def test_no_on_one_does_not_affect_other(self):
        from axiom.agents.raci import ProposalDecision, RACILedger

        ledger = RACILedger(now=lambda: 0.0)
        ledger.record_no("local-rag-steward")
        assert ledger.propose("local-rag-steward") is ProposalDecision.SKIP
        assert ledger.propose("ci-diagnose") is ProposalDecision.ASK


class TestPersistence:
    def test_round_trip_through_dict(self):
        from axiom.agents.raci import ProposalDecision, RACILedger

        ledger = RACILedger(now=lambda: 0.0)
        ledger.record_yes("local-rag-steward")
        ledger.record_no("ci-diagnose")

        snapshot = ledger.to_dict()
        restored = RACILedger.from_dict(snapshot, now=lambda: 0.0)

        assert restored.propose("local-rag-steward") is ProposalDecision.AUTO
        assert restored.propose("ci-diagnose") is ProposalDecision.SKIP

    def test_save_load_to_disk(self, tmp_path):
        from axiom.agents.raci import ProposalDecision, RACILedger

        path = tmp_path / "raci.json"
        ledger = RACILedger(now=lambda: 0.0)
        ledger.record_yes("local-rag-steward")
        ledger.save(path)

        restored = RACILedger.load(path, now=lambda: 0.0)
        assert restored.propose("local-rag-steward") is ProposalDecision.AUTO

    def test_load_missing_file_returns_empty_ledger(self, tmp_path):
        from axiom.agents.raci import ProposalDecision, RACILedger

        ledger = RACILedger.load(tmp_path / "does-not-exist.json", now=lambda: 0.0)
        assert ledger.propose("local-rag-steward") is ProposalDecision.ASK


class TestBaseAgentIntegration:
    def test_base_agent_has_raci_ledger(self):
        from axiom.agents.base_agent import BaseAgent

        agent = BaseAgent(agent_id="rivet")
        assert hasattr(agent, "raci")
        assert agent.raci is not None

    def test_base_agent_propose_routes_to_ledger(self):
        from axiom.agents.base_agent import BaseAgent
        from axiom.agents.raci import ProposalDecision

        agent = BaseAgent(agent_id="rivet")
        agent.raci.pre_approve("local-rag-steward")
        assert agent.propose_action("local-rag-steward") is ProposalDecision.AUTO
