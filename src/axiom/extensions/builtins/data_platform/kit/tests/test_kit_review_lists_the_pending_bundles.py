# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``kit-review`` lists the bundles waiting for a decision, and ``--all`` lists every one.

Found live: on a node that had taken a few dozen bundles, kit-review listed every one back to #1,
and the one waiting for kit-approve was somewhere in the middle.
"""

from __future__ import annotations

import logging

import pytest

from axiom.extensions.builtins.data_platform.kit import host
from axiom.extensions.builtins.data_platform.skills import kit_promotion
from axiom.infra.skills import SkillContext, SkillRegistry


@pytest.fixture
def asked(monkeypatch):
    seen: list = []
    monkeypatch.setattr(kit_promotion, "_node_dsn", lambda: "postgresql://node.invalid/x")
    monkeypatch.setattr(host, "review", lambda dsn, *, site=None, state=None: seen.append(state) or [])
    monkeypatch.setattr(host, "withdrawals", lambda dsn, *, site=None, state="pending": [])
    return seen


def _review(params, tmp_path):
    ctx = SkillContext(registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("t"))
    return kit_promotion.kit_review(params, ctx)


def test_kit_review_lists_the_pending_bundles_by_default_and_says_how_to_see_all(asked, tmp_path):
    r = _review({}, tmp_path)
    assert r.ok and asked == ["checked"]
    assert any("--all" in note for note in r.actions_taken), r.actions_taken


def test_all_lists_every_bundle_and_a_named_state_wins(asked, tmp_path):
    assert _review({"all": True}, tmp_path).ok
    assert _review({"state": "applied"}, tmp_path).ok
    assert asked == [None, "applied"]


def test_the_cli_takes_all():
    from axiom.extensions.builtins.data_platform.commands.cli import _args_to_params, _parser

    args = _parser().parse_args(["kit-review", "--all"])
    assert _args_to_params(args).get("all") is True
    assert "all" not in _args_to_params(_parser().parse_args(["kit-review"]))
