# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-048: brand-scoped extension visibility (the distribution model).

Discovery is universal (ADR-044 §D2.6); what each *brand* displays is scoped by
tier. The platform brand (`axi`) must not surface a domain product's extensions
(a domain consumer's `model_corral`); a domain distribution (`neut`) inherits the platform
base but not sibling products (Keplo). Genuine third-party plugins are never
portfolio members, so they stay universally visible (the Vyzier case).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from axiom.extensions import discovery as D


class TestNormPkg:
    def test_normalizes_case_and_underscores(self):
        assert D._norm_pkg("Domain_OS") == "domain-os"
        assert D._norm_pkg("  axiom-os-lm ") == "axiom-os-lm"


class TestIsHiddenSibling:
    MEMBERS = {"axiom-os-lm", "domain-os", "keplo"}

    def test_axi_hides_every_domain_sibling(self):
        # Active brand = platform: all portfolio domain products are above it.
        assert D._is_hidden_sibling("domain-os", "axiom-os-lm", self.MEMBERS)
        assert D._is_hidden_sibling("keplo", "axiom-os-lm", self.MEMBERS)

    def test_neut_keeps_itself_and_the_base_hides_only_other_siblings(self):
        assert not D._is_hidden_sibling("domain-os", "domain-os", self.MEMBERS)
        assert not D._is_hidden_sibling("axiom-os-lm", "domain-os", self.MEMBERS)
        assert D._is_hidden_sibling("keplo", "domain-os", self.MEMBERS)  # sibling

    def test_third_party_plugin_never_hidden(self):
        # Not a portfolio member -> a marketplace plugin -> always visible.
        assert not D._is_hidden_sibling("some-vyzier-plugin", "axiom-os-lm", self.MEMBERS)
        assert not D._is_hidden_sibling("some-vyzier-plugin", "domain-os", self.MEMBERS)

    def test_platform_base_never_hidden_even_as_member(self):
        assert not D._is_hidden_sibling("axiom-os-lm", "axiom-os-lm", self.MEMBERS)

    def test_name_normalization_applies(self):
        assert D._is_hidden_sibling("Domain_OS", "axiom-os-lm", {"domain-os"})


@dataclass
class _Member:
    package_name: str
    product_name: str = ""
    wrapper_binary: str = ""


def _patch_brand(monkeypatch, active_pkg, member_pkgs):
    # _brand_hidden_packages imports these from the branding module at call
    # time, so patching the module attributes is sufficient.
    from axiom.infra import branding

    monkeypatch.setattr(
        branding, "discover_portfolio_members",
        lambda: [_Member(p) for p in member_pkgs],
    )
    monkeypatch.setattr(
        branding, "get_branding",
        lambda: branding.BrandingConfig(package_name=active_pkg),
    )


class TestBrandHiddenPackages:
    def test_axi_hides_siblings(self, monkeypatch):
        _patch_brand(monkeypatch, "axiom-os-lm", {"axiom-os-lm", "domain-os", "keplo"})
        assert D._brand_hidden_packages() == {"domain-os", "keplo"}

    def test_neut_hides_only_other_siblings(self, monkeypatch):
        _patch_brand(monkeypatch, "domain-os", {"axiom-os-lm", "domain-os", "keplo"})
        assert D._brand_hidden_packages() == {"keplo"}

    def test_only_self_and_base_registered_hides_nothing(self, monkeypatch):
        _patch_brand(monkeypatch, "axiom-os-lm", {"axiom-os-lm"})
        assert D._brand_hidden_packages() == set()

    def test_fails_open_on_error(self, monkeypatch):
        from axiom.infra import branding

        def boom():
            raise RuntimeError("metadata exploded")

        monkeypatch.setattr(branding, "discover_portfolio_members", boom)
        assert D._brand_hidden_packages() == set()


class TestExtensionSourcePackage:
    @staticmethod
    def _ext(path: str):
        return SimpleNamespace(root=Path(path), name=path.rsplit("/", 1)[-1])

    def test_installed_sibling_package(self):
        ext = self._ext("/v/site-packages/domain_os/extensions/builtins/model_corral")
        assert D._extension_source_package(ext) == "domain_os"

    def test_platform_builtin(self):
        ext = self._ext("/v/src/axiom/extensions/builtins/hygiene")
        assert D._extension_source_package(ext) == "axiom"

    def test_project_or_user_dir_has_no_source_package(self):
        # Not under any package's extensions/builtins tree.
        assert D._extension_source_package(self._ext("/proj/.neut/extensions/myext")) is None


class TestSurfacedExtensions:
    """Discovery is universal (presence ≠ power); the *listing* view scopes."""

    def _exts(self):
        return [
            SimpleNamespace(root=Path("/v/domain_os/extensions/builtins/model_corral"), name="model_corral"),
            SimpleNamespace(root=Path("/v/axiom/extensions/builtins/hygiene"), name="hygiene"),
            SimpleNamespace(root=Path("/v/some_plugin/extensions/builtins/cool"), name="cool"),
        ]

    def test_nothing_hidden_returns_all(self, monkeypatch):
        exts = self._exts()
        monkeypatch.setattr(D, "discover_extensions", lambda *a: exts)
        monkeypatch.setattr(D, "_brand_hidden_packages", lambda: set())
        assert D.surfaced_extensions() == exts

    def test_hides_sibling_keeps_base_and_third_party(self, monkeypatch):
        exts = self._exts()
        monkeypatch.setattr(D, "discover_extensions", lambda *a: exts)
        monkeypatch.setattr(D, "_brand_hidden_packages", lambda: {"domain-os"})
        names = [e.name for e in D.surfaced_extensions()]
        assert "model_corral" not in names      # sibling distribution — hidden
        assert "hygiene" in names                # platform base — kept
        assert "cool" in names                   # third-party plugin — kept
