# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""A registered product's old state directory is reported until reconciled (#967, #2010).

#967 kept the doctor quiet about a directory a registered product's branding
produced, because that product read it as its live state directory. #2010
replaced the premise: every CLI name now reads one store, ``.axi``, so a
product's own former directory is no longer read either. Saying nothing would
hide exactly the settings an upgraded product node stopped seeing; the doctor
reports it and names ``state reconcile``, which copies (never moves) what it
holds and archives first, the harm #967 guarded against.
"""

from __future__ import annotations

from axiom.cli import doctor
from axiom.cli.doctor import CheckStatus
from axiom.infra import brand_migration
from axiom.infra.branding import PortfolioMember


def _members(*cli_names):
    return lambda: [PortfolioMember("pkg", "Prod", "bin", cli_name=n) for n in cli_names]


def _check(tmp_path):
    home = tmp_path / "home"
    return doctor.check_legacy_state_dir(project_root=tmp_path, home=home, state_dir=home / ".axi")


def test_a_registered_products_old_dir_is_reported_with_the_reconcile_verb(tmp_path, monkeypatch):
    legacy_name = brand_migration.LEGACY_PROJECT_DIR
    (tmp_path / legacy_name).mkdir()
    monkeypatch.setattr(
        "axiom.infra.branding.discover_portfolio_members", _members(legacy_name.lstrip("."))
    )
    res = _check(tmp_path)
    assert res.status is CheckStatus.WARNING
    assert "state reconcile" in res.fix_hint


def test_an_unregistered_legacy_dir_still_warns(tmp_path, monkeypatch):
    (tmp_path / brand_migration.LEGACY_PROJECT_DIR).mkdir()
    monkeypatch.setattr("axiom.infra.branding.discover_portfolio_members", _members("other"))
    res = _check(tmp_path)
    assert res.status is CheckStatus.WARNING


def test_warn_if_legacy_dir_is_silent_for_a_registered_name(tmp_path, monkeypatch, caplog):
    legacy_name = brand_migration.LEGACY_PROJECT_DIR
    (tmp_path / legacy_name).mkdir()
    monkeypatch.setattr(
        "axiom.infra.branding.discover_portfolio_members", _members(legacy_name.lstrip("."))
    )
    brand_migration.warn_if_legacy_dir(tmp_path / ".axi")
    assert legacy_name not in caplog.text
