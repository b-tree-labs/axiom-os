# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A product's CLI and an MCP server scan the same extension directories, in the same order.

Found in a colleague's first hour: the MCP server, started under the bare platform name, scanned
``~/.axi/extensions`` while the product's CLI scanned ``~/.<product>/extensions``. A tool the person
added showed in one and was an "unknown tool" in the other.

The order is documented in ``discovery.get_extension_dirs``: project-local, then user-level, for the
installed products' names sorted and then ``axi``; installed packages; builtins.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.extensions import discovery
from axiom.infra import branding
from axiom.infra.branding import BrandingConfig, PortfolioMember

PRODUCT = "prodx"  # an invented product command name


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("AXI_STATE_DIR", raising=False)
    monkeypatch.delenv("AXIOM_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(discovery, "_installed_package_extension_dirs", lambda: [])
    monkeypatch.setattr(
        branding,
        "discover_portfolio_members",
        lambda: [
            PortfolioMember("axiom-os-lm", "Axiom", "Axiom-Background-Service", "axi"),
            PortfolioMember("prodx-pkg", "ProdX", "ProdX-Background-Service", PRODUCT),
        ],
    )
    from axiom.extensions import index_cache

    monkeypatch.setattr(index_cache, "enabled", lambda: False)
    yield home
    branding.reset()


def _add_extension(root: Path, name: str) -> None:
    ext = root / name
    ext.mkdir(parents=True)
    (ext / "axiom-extension.toml").write_text(
        f'[extension]\nname = "{name}"\nversion = "0.1.0"\ndescription = "a person\'s own"\n',
        encoding="utf-8",
    )


def _as_platform() -> list[Path]:
    branding.reset()
    return discovery.get_extension_dirs()


def _as_product() -> list[Path]:
    branding.reset()
    branding.register(BrandingConfig(cli_name=PRODUCT, product_name="ProdX"))
    try:
        return discovery.get_extension_dirs()
    finally:
        branding.reset()


def test_both_entry_points_scan_the_same_tiers_in_the_same_order(home):
    for name in (f".{PRODUCT}", ".axi"):
        (home / name / "extensions").mkdir(parents=True)

    platform, product = _as_platform(), _as_product()

    assert platform == product
    users = [d for d in platform if d.is_relative_to(home)]
    assert users == [home / f".{PRODUCT}" / "extensions", home / ".axi" / "extensions"]


@pytest.mark.parametrize("added_under", [f".{PRODUCT}", ".axi"])
def test_an_extension_a_person_adds_is_seen_by_both(home, added_under):
    _add_extension(home / added_under / "extensions", "my-own-tool")

    seen_by_platform = {e.name for e in discovery.discover_extensions(*_as_platform())}
    seen_by_product = {e.name for e in discovery.discover_extensions(*_as_product())}

    assert "my-own-tool" in seen_by_platform
    assert "my-own-tool" in seen_by_product


def test_an_isolated_state_dir_is_the_only_user_tier(home, tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "isolated"))
    assert discovery.user_extension_dirs() == [tmp_path / "isolated" / "extensions"]
