# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for CLI availability gating (axiom.infra.cli_gating)."""

from __future__ import annotations

import pytest

from axiom.infra import capabilities, cli_gating


@pytest.fixture(autouse=True)
def _clear_cache():
    capabilities.clear_cache()
    yield
    capabilities.clear_cache()


def test_available_when_no_requires():
    assert cli_gating.is_available([]) is True
    assert cli_gating.is_available(None) is True


def test_unavailable_when_requirement_missing(monkeypatch):
    monkeypatch.setattr(capabilities.shutil, "which", lambda _b: None)
    assert cli_gating.is_available(["git"]) is False
    unmet = cli_gating.unmet_requirements(["git"])
    assert [c.name for c, _ in unmet] == ["git"]


def test_available_when_requirement_met(monkeypatch):
    monkeypatch.setattr(capabilities.shutil, "which", lambda _b: "/usr/bin/git")
    assert cli_gating.is_available(["git"]) is True


def test_unknown_capability_name_is_skipped():
    # Forward-compat: an unrecognized name must not block the command.
    assert cli_gating.is_available(["totally-unknown-cap"]) is True


def test_format_unavailable_includes_reason_and_remedy(monkeypatch):
    monkeypatch.setattr(capabilities.shutil, "which", lambda _b: None)
    unmet = cli_gating.unmet_requirements(["git"])
    msg = cli_gating.format_unavailable("release", unmet)
    assert "release" in msg
    assert "git" in msg.lower()
    assert "git-scm.com" in msg  # the remedy line
