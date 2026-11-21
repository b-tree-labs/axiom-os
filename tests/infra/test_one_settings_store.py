# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One node, one settings store, whichever CLI name was typed (#2010).

A product built on the platform registers its own CLI name. That name brands the
command; it used to split the state as well, because the user and project state
folders were spelled ``.<cli_name>``: a setting made through the platform's CLI was
not seen through the product's, on the same node. The platform folder is now the one
store for every CLI name. Temporary HOME and project folders only.
"""

from __future__ import annotations

import pytest

from axiom.infra import branding
from axiom.infra.brand_migration import LEGACY_PROJECT_DIR

#: A registered product CLI name, as a fixture. The retired folder name is the
#: realistic case: it is the one a product's state was written under.
PRODUCT_CLI = LEGACY_PROJECT_DIR[1:]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temporary HOME with no state-dir override, so the default is exercised."""
    from pathlib import Path

    monkeypatch.delenv("AXI_STATE_DIR", raising=False)
    monkeypatch.delenv("AXIOM_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    branding.reset()
    yield tmp_path
    branding.reset()


def _register_product() -> None:
    branding.register(branding.BrandingConfig(cli_name=PRODUCT_CLI, product_name="Product"))


def _store(monkeypatch, project):
    """A settings store whose project scope is *project* (the store binds it at import)."""
    from axiom.extensions.builtins.settings import store as store_mod
    from axiom.infra.paths import get_project_state_dir

    monkeypatch.setattr(
        store_mod, "_PROJECT_SETTINGS_PATH", get_project_state_dir(project) / "settings.toml"
    )
    return store_mod.SettingsStore()


@pytest.mark.acceptance("2010-A1")
def test_a_setting_written_through_the_platform_cli_is_read_under_a_product_cli_name(
    home, monkeypatch
):
    project = home / "repo"
    (project / ".git").mkdir(parents=True)

    _store(monkeypatch, project).set("routing.default_mode", "local", scope="global")
    _store(monkeypatch, project).set("user.org", "example.org", scope="project")

    _register_product()
    product = _store(monkeypatch, project)
    assert product.get("routing.default_mode") == "local"
    assert product.get("user.org") == "example.org"


@pytest.mark.acceptance("2010-A1")
def test_a_setting_written_under_a_product_cli_name_is_read_through_the_platform_cli(
    home, monkeypatch
):
    project = home / "repo"
    (project / ".git").mkdir(parents=True)

    _register_product()
    _store(monkeypatch, project).set("routing.default_mode", "local", scope="global")
    _store(monkeypatch, project).set("user.org", "example.org", scope="project")

    branding.reset()
    platform = _store(monkeypatch, project)
    assert platform.get("routing.default_mode") == "local"
    assert platform.get("user.org") == "example.org"


@pytest.mark.acceptance("2010-A2")
def test_one_process_resolves_the_same_folders_before_and_after_a_product_registers(home):
    """Extension discovery runs before a product registers its branding, and the
    rest of the command after: both halves must name the same folders."""
    from axiom.infra import paths

    project = home / "repo"
    (project / ".git").mkdir(parents=True)

    before = (
        paths.get_user_state_dir(),
        paths.get_platform_state_dir(),
        paths.get_project_state_dir(project),
        paths.project_dir_name(),
    )
    _register_product()
    after = (
        paths.get_user_state_dir(),
        paths.get_platform_state_dir(),
        paths.get_project_state_dir(project),
        paths.project_dir_name(),
    )

    assert before == after
    assert after[0] == home / ".axi"
    assert after[0] == after[1], "user and platform state are one folder"
    assert after[2] == project / ".axi"


@pytest.mark.acceptance("2010-A2")
def test_the_override_still_redirects_every_cli_name(home, monkeypatch):
    from axiom.infra import paths

    monkeypatch.setenv("AXI_STATE_DIR", str(home / "elsewhere"))
    _register_product()
    assert paths.get_user_state_dir() == home / "elsewhere"
    assert paths.get_platform_state_dir() == home / "elsewhere"
