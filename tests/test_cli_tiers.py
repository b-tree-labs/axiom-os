# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for progressive disclosure CLI tiers."""

from __future__ import annotations

import pytest

from axiom.infra.cli_tiers import (
    _compute_tier,
    filter_subparsers_for_help,
    get_command_tier,
    get_user_tier,
    record_action,
    set_user_tier,
    should_show_command,
)


@pytest.fixture(autouse=True)
def _isolate_profile(tmp_path, monkeypatch):
    """Redirect profile path to tmp_path for all tests."""
    profile = tmp_path / "profile.json"
    monkeypatch.setattr("axiom.infra.cli_tiers._profile_path", lambda: profile)


def test_new_user_tier_is_zero():
    assert get_user_tier() == 0


def test_set_user_tier_persists(tmp_path):
    set_user_tier(3)
    assert get_user_tier() == 3


def test_set_user_tier_clamps():
    set_user_tier(99)
    assert get_user_tier() == 4
    set_user_tier(-5)
    assert get_user_tier() == 0


def test_record_action_advances_to_tier_1():
    assert get_user_tier() == 0
    record_action("model", "add")
    assert get_user_tier() == 1


def test_record_action_advances_to_tier_2():
    record_action("model", "add")
    record_action("federation", "init")
    assert get_user_tier() == 2


def test_record_action_advances_to_tier_3():
    record_action("model", "add")
    record_action("federation", "init")
    record_action("research", "create")
    assert get_user_tier() == 3


def test_record_action_advances_to_tier_4():
    record_action("model", "add")
    record_action("federation", "init")
    record_action("research", "create")
    record_action("chaos", "run")
    assert get_user_tier() == 4


def test_should_show_command_respects_tier():
    # Tier 0 user should not see tier 3 commands
    assert should_show_command("model", "init", user_tier=0) is True
    assert should_show_command("research", "create", user_tier=0) is False
    assert should_show_command("research", "create", user_tier=3) is True


def test_filter_subparsers_for_help():
    subparsers = {
        "init": "parser_init",
        "validate": "parser_validate",
        "sweep": "parser_sweep",      # tier 2
        "share": "parser_share",      # tier 2
    }
    filtered = filter_subparsers_for_help("model", subparsers, user_tier=0)
    assert "init" in filtered
    assert "validate" in filtered
    assert "sweep" not in filtered
    assert "share" not in filtered

    filtered_t2 = filter_subparsers_for_help("model", subparsers, user_tier=2)
    assert "sweep" in filtered_t2
    assert "share" in filtered_t2


def test_get_command_tier_known():
    assert get_command_tier("model", "init") == 0
    assert get_command_tier("model", "clone") == 1
    assert get_command_tier("federation", "status") == 2
    assert get_command_tier("research", "create") == 3
    assert get_command_tier("chaos", "list") == 4


def test_get_command_tier_noun_level():
    # "status:" is a noun-level default
    assert get_command_tier("status", "anything") == 0


def test_unknown_command_defaults_to_tier_0():
    assert get_command_tier("totally_unknown", "cmd") == 0


def test_tier_progression():
    actions = []
    assert _compute_tier(actions) == 0

    actions.append("model:add")
    assert _compute_tier(actions) == 1

    actions.append("nodes:add")
    assert _compute_tier(actions) == 2

    actions.append("security:scan")
    assert _compute_tier(actions) == 3

    actions.append("chaos:run")
    assert _compute_tier(actions) == 4


def test_manual_tier_overrides_auto():
    record_action("model", "add")
    assert get_user_tier() == 1
    set_user_tier(4)
    assert get_user_tier() == 4
