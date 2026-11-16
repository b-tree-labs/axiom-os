# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""A directory a registered product's branding produces is not legacy (#967).

The retired name is a consumer name, but a downstream product whose
`cli_name` yields that same name uses it as its live state directory. Telling
its operators to move state out would be wrong. Any other legacy dir still warns.
"""

from __future__ import annotations

from axiom.cli import doctor
from axiom.cli.doctor import CheckStatus
from axiom.infra import brand_migration
from axiom.infra.branding import PortfolioMember


def _members(*cli_names):
    return lambda: [PortfolioMember("pkg", "Prod", "bin", cli_name=n) for n in cli_names]


def test_a_registered_products_dir_gets_no_warning(tmp_path, monkeypatch):
    legacy_name = brand_migration.LEGACY_PROJECT_DIR
    (tmp_path / legacy_name).mkdir()
    monkeypatch.setattr(
        "axiom.infra.branding.discover_portfolio_members", _members(legacy_name.lstrip("."))
    )
    assert legacy_name in brand_migration.branded_project_dir_names()
    res = doctor.check_legacy_state_dir(project_root=tmp_path)
    assert res.status is CheckStatus.OK


def test_an_unregistered_legacy_dir_still_warns(tmp_path, monkeypatch):
    (tmp_path / brand_migration.LEGACY_PROJECT_DIR).mkdir()
    monkeypatch.setattr("axiom.infra.branding.discover_portfolio_members", _members("other"))
    res = doctor.check_legacy_state_dir(project_root=tmp_path)
    assert res.status is CheckStatus.WARNING


def test_warn_if_legacy_dir_is_silent_for_a_registered_name(tmp_path, monkeypatch, caplog):
    legacy_name = brand_migration.LEGACY_PROJECT_DIR
    (tmp_path / legacy_name).mkdir()
    monkeypatch.setattr(
        "axiom.infra.branding.discover_portfolio_members", _members(legacy_name.lstrip("."))
    )
    brand_migration.warn_if_legacy_dir(tmp_path / ".axi")
    assert legacy_name not in caplog.text
