# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Tests for per-principal agent settings (ownership-gated whitelist)."""
from __future__ import annotations

import pytest

from axiom.extensions.builtins.connect.agent_settings import (
    NotAuthorized,
    StepUpRequired,
    get_setting,
    set_setting,
)
from axiom.memory.ownership import new_ownership


def test_get_setting_missing_returns_none(tmp_path):
    assert get_setting("@rivet:lab", "owner.nickname", path=tmp_path / "s.json") is None


def test_master_sets_and_reads_back(tmp_path):
    p = tmp_path / "s.json"
    own = new_ownership("@ben:lab")
    res = set_setting(
        "@rivet:lab", "owner.nickname", "Rivet",
        requester="@ben:lab", ownership=own, path=p,
    )
    assert res["value"] == "Rivet"
    assert get_setting("@rivet:lab", "owner.nickname", path=p) == "Rivet"


def test_unknown_key_raises_keyerror(tmp_path):
    own = new_ownership("@ben:lab")
    with pytest.raises(KeyError):
        set_setting(
            "@rivet:lab", "no.such.key", 1,
            requester="@ben:lab", ownership=own, path=tmp_path / "s.json",
        )


def test_unauthorized_requester_rejected(tmp_path):
    own = new_ownership("@ben:lab")
    with pytest.raises(NotAuthorized):
        set_setting(
            "@rivet:lab", "owner.nickname", "X",
            requester="@mallory:lab", ownership=own, path=tmp_path / "s.json",
        )


def test_sensitive_key_requires_step_up(tmp_path):
    p = tmp_path / "s.json"
    own = new_ownership("@ben:lab")
    # Inherited authority alone is not enough for a sensitive key.
    with pytest.raises(StepUpRequired):
        set_setting(
            "@rivet:lab", "secrets.rotate", True,
            requester="@ben:lab", ownership=own, path=p,
        )
    # With step-up it goes through.
    res = set_setting(
        "@rivet:lab", "secrets.rotate", True,
        requester="@ben:lab", ownership=own, stepped_up=True, path=p,
    )
    assert res["key"] == "secrets.rotate"
    assert get_setting("@rivet:lab", "secrets.rotate", path=p) is True
