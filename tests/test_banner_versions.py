# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The banner names the right versions, and says which is which.

It printed the PLATFORM's version under whatever brand was registered, so an
adopter of a branded distribution read a number that was not their product's
and had no way to tell. A version string is the first sentence of every bug
report, and that one was wrong.

Three numbers can be in play on a bound node and they move independently:
the site, the product, and the platform.
"""

from __future__ import annotations

import pytest

from axiom.axiom_cli import banner_versions, render_banner_versions
from axiom.infra import branding


@pytest.fixture(autouse=True)
def _restore_branding():
    saved = list(branding._registered)
    try:
        yield
    finally:
        branding._registered[:] = saved


def _register(**kw):
    branding.register(branding.BrandingConfig(
        cli_name=kw.pop("cli_name", "acme"),
        product_name=kw.pop("product_name", "Acme OS"),
        package_name=kw.pop("package_name", "axiom-os-lm"),
        **kw,
    ))


class TestUnbrandedIsUnchanged:
    def test_the_platform_alone_renders_as_a_bare_version(self):
        assert render_banner_versions().startswith("v")

    def test_it_never_raises(self):
        banner_versions()


class TestABrandShowsItsOwnNumberAndThePlatforms:
    def test_both_appear_and_both_are_labelled(self):
        """`pytest` is a real installed distribution that is not the
        platform, which is what this needs: a brand whose package genuinely
        resolves to a different version. Any installed non-platform package
        would do, and naming one avoids naming a consumer."""
        _register(cli_name="brandcli", package_name="pytest")
        line = render_banner_versions()
        assert line.startswith("brandcli ")
        assert "axiom " in line

    def test_a_brand_whose_package_IS_the_platform_renders_bare(self):
        """Such a brand is not a separate product, so there is one number
        and labelling it would imply there were two. Getting this wrong
        dropped both rows and fell back to a bare version by accident, which
        looked right for the wrong reason."""
        _register(cli_name="acme", package_name="axiom-os-lm")
        line = render_banner_versions()
        assert line.startswith("v")
        assert "acme" not in line

    def test_a_brand_whose_package_is_not_installed_is_skipped_not_crashed(self):
        _register(cli_name="ghost", package_name="definitely-not-installed-xyz")
        line = render_banner_versions()
        assert "ghost" not in line
        assert line, "the platform's version should still be there"


class TestASiteVersionComesFromTheBrand:
    """The platform has no business knowing what a site is, so the site
    entry is supplied by whatever brand does."""

    def test_extras_are_shown_first(self):
        _register(version_extras_fn=lambda: [("site-a", "1.6.74")])
        assert banner_versions()[0] == ("site-a", "1.6.74")

    def test_the_rendered_line_reads_broadest_context_first(self):
        _register(version_extras_fn=lambda: [("site-a", "1.6.74")])
        line = render_banner_versions()
        assert line.startswith("site-a 1.6.74")
        assert "axiom" in line

    def test_a_brand_that_raises_does_not_break_the_banner(self):
        """A version line is a courtesy. It must never be why a CLI fails
        to start."""

        def boom():
            raise RuntimeError("no")

        _register(version_extras_fn=boom)
        assert render_banner_versions()

    def test_a_brand_with_no_site_adds_nothing(self):
        _register(version_extras_fn=lambda: [])
        assert "site-a" not in render_banner_versions()


class TestVersionFlagAgreesWithTheBanner:
    """`--version` had its own lookup and printed ONE number.

    So the banner could say `neut 1.9.0 · axiom 0.47.0` while `--version`
    said `neut 1.9.0` on the same install — and `--version` is the surface
    an operator runs when filing a bug. Two derivations of the same fact
    are two chances to be wrong about it.
    """

    def test_it_reports_every_component_the_banner_does(self):
        from axiom.axiom_cli import version_report

        _register(cli_name="brandcli", package_name="pytest")
        lines = version_report().splitlines()
        assert len(lines) == len(banner_versions())
        assert lines[0].startswith("brandcli ")
        assert any(line.startswith("axiom ") for line in lines)

    def test_a_site_row_from_the_brand_reaches_it(self):
        """The reason this matters: a bound node has three numbers, and the
        site's is the one nobody else can look up."""
        from axiom.axiom_cli import version_report

        _register(
            cli_name="brandcli",
            package_name="pytest",
            version_extras_fn=lambda: [("somesite", "1.6.74")],
        )
        lines = version_report().splitlines()
        assert lines[0] == "somesite 1.6.74"
        assert len(lines) == 3

    def test_every_line_is_labelled_so_it_stays_greppable(self):
        from axiom.axiom_cli import version_report

        _register(cli_name="brandcli", package_name="pytest")
        for line in version_report().splitlines():
            label, _, ver = line.partition(" ")
            assert label and ver, line

    def test_the_unbranded_platform_still_names_itself(self):
        """Unbranded, `banner_versions` labels the only row "" because the
        banner renders it as `v0.47.0`. `--version` has no such context, so
        a bare number would be the same defect in a new place."""
        from axiom.axiom_cli import version_report

        line = version_report()
        label, _, ver = line.partition(" ")
        assert label and ver, line

    def test_it_never_raises(self, monkeypatch):
        from axiom import axiom_cli

        def _boom():
            raise RuntimeError("metadata is gone")

        monkeypatch.setattr(axiom_cli, "banner_versions", _boom)
        assert axiom_cli.version_report() == "unknown"
