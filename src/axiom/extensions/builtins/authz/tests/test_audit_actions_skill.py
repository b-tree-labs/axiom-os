# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi audit actions`` — action-provenance query verb (#665).

Per ADR-056 the verb is a thin wrapper over the ``audit.actions`` skill,
which itself delegates to ``axiom.policy.action_ledger``. These tests
exercise the skill + parser against a JSONL ledger — no live PG.
"""

from __future__ import annotations

import logging

import pytest

from axiom.infra.skills import SkillContext, SkillRegistry


@pytest.fixture
def seeded_state_dir(tmp_path, monkeypatch):
    from axiom.policy import action_ledger
    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.delenv("AXIOM_AUDIT_HMAC_KEY", raising=False)
    action_ledger._reset_backend_cache()
    ledger = action_ledger.ActionLedger(state_dir=tmp_path)
    ledger.record_action(
        agent="tidy", op_class="git.branch.delete", name="prune_merged",
        candidate="feat/a", guards=["act"], outcome="proceeded",
    )
    ledger.record_action(
        agent="rivet", op_class="github.issue.close", name="auto_close",
        candidate="#42", guards=["hard_disable"], outcome="refused",
        refusing_rule="hard_disable",
    )
    yield tmp_path
    action_ledger._reset_backend_cache()


def _ctx(state_dir) -> SkillContext:
    from axiom.extensions.builtins.authz import skills as audit_skills
    reg = SkillRegistry()
    audit_skills.bind(reg)
    return SkillContext(
        registry=reg,
        state_dir=state_dir,
        logger=logging.getLogger("test.audit.actions"),
        user_prompt=None,
    )


class TestSkill:

    def test_actions_verb_is_registered(self):
        from axiom.extensions.builtins.authz import skills as audit_skills
        assert "actions" in audit_skills.verbs()

    def test_list_all(self, seeded_state_dir):
        ctx = _ctx(seeded_state_dir)
        result = ctx.registry.invoke("audit.actions", {}, ctx)
        assert result.ok
        assert result.value["resource"] == "actions"
        assert result.value["count"] == 2

    def test_filters(self, seeded_state_dir):
        ctx = _ctx(seeded_state_dir)
        result = ctx.registry.invoke(
            "audit.actions",
            {"agent": "rivet", "outcome": "refused"},
            ctx,
        )
        assert result.ok
        items = result.value["items"]
        assert len(items) == 1
        assert items[0]["refusing_rule"] == "hard_disable"

    def test_bad_since_is_clean_error(self, seeded_state_dir):
        ctx = _ctx(seeded_state_dir)
        result = ctx.registry.invoke(
            "audit.actions", {"since": "garbage"}, ctx,
        )
        assert not result.ok
        assert result.errors

    def test_verify_chain(self, seeded_state_dir):
        ctx = _ctx(seeded_state_dir)
        result = ctx.registry.invoke("audit.actions", {"verify": True}, ctx)
        assert result.ok
        assert result.value["resource"] == "actions_verify"
        assert result.value["ok"] is True


class TestParser:

    def test_parser_accepts_all_flags(self):
        from axiom.extensions.builtins.authz.cli import _build_parser
        args = _build_parser().parse_args([
            "actions",
            "--agent", "tidy",
            "--op-class", "git.branch.delete",
            "--outcome", "refused",
            "--since", "2d",
            "--until", "1h",
            "--limit", "5",
        ])
        assert args.verb == "actions"
        assert args.agent == "tidy"
        assert args.op_class == "git.branch.delete"
        assert args.outcome == "refused"
        assert args.since == "2d"
        assert args.until == "1h"
        assert args.limit == 5

    def test_parser_accepts_verify(self):
        from axiom.extensions.builtins.authz.cli import _build_parser
        args = _build_parser().parse_args(["actions", "--verify"])
        assert args.verify is True
