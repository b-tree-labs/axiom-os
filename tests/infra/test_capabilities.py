# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the capability-probing framework (axiom.infra.capabilities)."""

from __future__ import annotations

import pytest

from axiom.infra import capabilities as caps
from axiom.infra.capabilities import Availability, Capability


@pytest.fixture(autouse=True)
def _clear_cache():
    caps.clear_cache()
    yield
    caps.clear_cache()


def test_binary_probe_available(monkeypatch):
    monkeypatch.setattr(caps.shutil, "which", lambda b: "/usr/bin/git")
    av = caps.check(caps.GIT)
    assert av.available is True
    assert av.reason == ""


def test_binary_probe_unavailable_has_reason_and_remedy(monkeypatch):
    monkeypatch.setattr(caps.shutil, "which", lambda b: None)
    av = caps.check(caps.GIT)
    assert av.available is False
    assert "git" in av.reason.lower()
    assert av.remedy  # non-empty install hint


def test_check_accepts_name_string(monkeypatch):
    monkeypatch.setattr(caps.shutil, "which", lambda b: "/x")
    assert caps.check("git").available is True


def test_is_available_shortcut(monkeypatch):
    monkeypatch.setattr(caps.shutil, "which", lambda b: None)
    assert caps.is_available(caps.GH_CLI) is False


def test_probe_result_is_cached(monkeypatch):
    calls = {"n": 0}

    def fake_which(_b):
        calls["n"] += 1
        return "/usr/bin/git"

    monkeypatch.setattr(caps.shutil, "which", fake_which)
    caps.check(caps.GIT)
    caps.check(caps.GIT)
    assert calls["n"] == 1  # second hit served from cache


def test_refresh_bypasses_cache(monkeypatch):
    calls = {"n": 0}

    def fake_which(_b):
        calls["n"] += 1
        return None

    monkeypatch.setattr(caps.shutil, "which", fake_which)
    caps.check(caps.GIT)
    caps.check(caps.GIT, refresh=True)
    assert calls["n"] == 2


def test_gitlab_token_from_env(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xxx")
    assert caps.check(caps.GITLAB_TOKEN).available is True


def test_gitlab_token_absent(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setattr(caps, "_glab_config_token", lambda: "")
    av = caps.check(caps.GITLAB_TOKEN)
    assert av.available is False
    assert av.remedy


def test_missing_returns_only_unmet(monkeypatch):
    monkeypatch.setattr(caps.shutil, "which", lambda b: "/x" if b == "git" else None)
    unmet = caps.missing([caps.GIT, caps.GH_CLI])
    assert [c.name for c, _ in unmet] == ["gh"]  # git met, gh unmet


def test_missing_empty_when_all_met(monkeypatch):
    monkeypatch.setattr(caps.shutil, "which", lambda b: "/x")
    assert caps.missing([caps.GIT, caps.GH_CLI]) == []


def test_register_and_get_custom_capability():
    custom = Capability("my-thing", lambda: Availability(True), "a custom dep")
    caps.register(custom)
    assert caps.get("my-thing") is custom
    assert caps.check("my-thing").available is True


def test_get_unknown_returns_none():
    assert caps.get("does-not-exist") is None


def test_check_unknown_name_raises():
    with pytest.raises(KeyError):
        caps.check("nope")
