# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``axiom.governance.mode`` and the production-mode safeguard."""

from __future__ import annotations

import pytest

from axiom.governance.mode import current_mode, is_dev, is_production
from axiom.governance.simple import (
    DevModeInProductionError,
    setup_extension,
)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    monkeypatch.delenv("AXIOM_MODE", raising=False)
    monkeypatch.delenv("AXIOM_ALLOW_DEV_MODE_IN_PRODUCTION", raising=False)


# ---------------------------------------------------------------------------
# current_mode()
# ---------------------------------------------------------------------------


def test_default_mode_is_dev():
    assert current_mode() == "dev"
    assert is_dev()


def test_production_mode_recognized(monkeypatch):
    monkeypatch.setenv("AXIOM_MODE", "production")
    assert current_mode() == "production"
    assert is_production()
    assert not is_dev()


def test_staging_mode_recognized(monkeypatch):
    monkeypatch.setenv("AXIOM_MODE", "staging")
    assert current_mode() == "staging"
    assert not is_dev()
    assert not is_production()


def test_unknown_mode_falls_back_to_dev_with_warning(monkeypatch, caplog):
    monkeypatch.setenv("AXIOM_MODE", "gibberish")
    with caplog.at_level("WARNING"):
        assert current_mode() == "dev"
    assert any("not one of" in r.message for r in caplog.records)


def test_case_insensitive(monkeypatch):
    monkeypatch.setenv("AXIOM_MODE", "PRODUCTION")
    assert current_mode() == "production"


def test_whitespace_stripped(monkeypatch):
    monkeypatch.setenv("AXIOM_MODE", "  production  ")
    assert current_mode() == "production"


# ---------------------------------------------------------------------------
# setup_extension dev_mode safeguards
# ---------------------------------------------------------------------------


class TestDevModeInProduction:
    def test_dev_mode_true_in_production_raises(self, monkeypatch):
        monkeypatch.setenv("AXIOM_MODE", "production")
        with pytest.raises(DevModeInProductionError, match="leak into prod"):
            setup_extension(
                "test-ext-prod-block",
                verbs=["invoke"],
                dev_mode=True,
                wire_authz=False, wire_vault=False,
            )

    def test_dev_mode_true_in_production_with_override_warns(
        self, monkeypatch, caplog
    ):
        monkeypatch.setenv("AXIOM_MODE", "production")
        monkeypatch.setenv("AXIOM_ALLOW_DEV_MODE_IN_PRODUCTION", "1")
        with caplog.at_level("WARNING"):
            ctx = setup_extension(
                "test-ext-prod-override",
                verbs=["invoke"],
                dev_mode=True,
                wire_authz=False, wire_vault=False,
            )
        assert ctx.dev_mode is True
        assert any("override active" in r.message for r in caplog.records)

    def test_dev_mode_true_in_staging_warns_but_allowed(
        self, monkeypatch, caplog
    ):
        monkeypatch.setenv("AXIOM_MODE", "staging")
        with caplog.at_level("WARNING"):
            ctx = setup_extension(
                "test-ext-staging",
                verbs=["invoke"],
                dev_mode=True,
                wire_authz=False, wire_vault=False,
            )
        assert ctx.dev_mode is True
        assert any("verify this is intentional" in r.message for r in caplog.records)

    def test_dev_mode_none_inherits_runtime_dev(self, monkeypatch):
        # AXIOM_MODE absent → dev → dev_mode auto-True
        ctx = setup_extension(
            "test-ext-default-dev",
            verbs=["invoke"],
            dev_mode=None,
            wire_authz=False, wire_vault=False,
        )
        assert ctx.dev_mode is True

    def test_dev_mode_none_inherits_runtime_production(self, monkeypatch):
        # AXIOM_MODE=production → dev_mode auto-False (no exception)
        monkeypatch.setenv("AXIOM_MODE", "production")
        ctx = setup_extension(
            "test-ext-default-prod",
            verbs=["invoke"],
            dev_mode=None,
            wire_authz=False, wire_vault=False,
        )
        assert ctx.dev_mode is False

    def test_dev_mode_false_in_production_is_fine(self, monkeypatch):
        monkeypatch.setenv("AXIOM_MODE", "production")
        ctx = setup_extension(
            "test-ext-prod-clean",
            verbs=["invoke"],
            dev_mode=False,
            wire_authz=False, wire_vault=False,
        )
        assert ctx.dev_mode is False
