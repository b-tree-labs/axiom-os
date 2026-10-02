# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for portfolio entry-points discovery + cross-brand Background Service cleanup."""

from __future__ import annotations

from unittest.mock import patch

from axiom.infra.branding import (
    PortfolioMember,
    _portfolio_metadata,
    discover_portfolio_members,
)


class TestPortfolioMetadata:
    def test_axiom_os_self_declaration_shape(self):
        meta = _portfolio_metadata()
        assert meta["package_name"] == "axiom-os-lm"
        assert meta["product_name"] == "Axiom"
        assert meta["wrapper_binary"] == "Axiom-Background-Service"

    def test_discover_portfolio_members_includes_axiom_os(self):
        members = discover_portfolio_members()
        names = {m.package_name for m in members}
        # axiom-os must be discoverable in this venv (it's installed editable here)
        assert "axiom-os-lm" in names

    def test_discovered_members_are_PortfolioMember_records(self):
        members = discover_portfolio_members()
        for m in members:
            assert isinstance(m, PortfolioMember)
            assert m.package_name
            assert m.product_name
            assert m.wrapper_binary


class TestCrossBrandCleanup:
    def test_legacy_launchd_cleans_per_agent_plists_for_all_portfolio_members(self, tmp_path, monkeypatch):
        """Pre-0.11.1 per-agent plists from any portfolio member are cleaned up."""
        import pathlib

        from axiom.extensions.builtins.agents import cli as agents_cli

        launch_agents_dir = tmp_path / "Library" / "LaunchAgents"
        launch_agents_dir.mkdir(parents=True)
        (launch_agents_dir / "com.axiom-os-lm.release-agent.plist").write_text("legacy")
        (launch_agents_dir / "com.axiom-os-lm.diagnostics-agent.plist").write_text("legacy")
        (launch_agents_dir / "com.axiom-os-lm.background-service.plist").write_text("current")
        (launch_agents_dir / "com.unrelated.thing.plist").write_text("not portfolio")

        # Patch portfolio_members at the source module so the lazy import resolves to it
        monkeypatch.setattr(
            "axiom.infra.branding.discover_portfolio_members",
            lambda: [
                PortfolioMember(
                    package_name="axiom-os-lm",
                    product_name="Axiom",
                    wrapper_binary="Axiom-Background-Service",
                )
            ],
        )

        with patch.object(pathlib.Path, "home", return_value=tmp_path):
            with patch("subprocess.run"):
                results = agents_cli._cleanup_legacy_launchd()

        # Both per-agent plists removed
        assert not (launch_agents_dir / "com.axiom-os-lm.release-agent.plist").exists()
        assert not (launch_agents_dir / "com.axiom-os-lm.diagnostics-agent.plist").exists()
        # Current Background Service plist preserved
        assert (launch_agents_dir / "com.axiom-os-lm.background-service.plist").exists()
        # Unrelated plist untouched
        assert (launch_agents_dir / "com.unrelated.thing.plist").exists()
        # Two cleanup results
        cleanup_names = [r.agent_name for r in results if r.agent_name.startswith("<legacy-cleanup:")]
        assert len(cleanup_names) == 2

    def test_cross_brand_background_service_plist_is_cleaned(self, tmp_path, monkeypatch):
        """If domain-consumer is current brand and com.axiom-os-lm.background-service.plist
        exists from a prior install, it gets cleaned up."""
        import pathlib

        from axiom.extensions.builtins.agents import cli as agents_cli
        from axiom.infra.branding import BrandingConfig

        launch_agents_dir = tmp_path / "Library" / "LaunchAgents"
        launch_agents_dir.mkdir(parents=True)
        (launch_agents_dir / "com.axiom-os-lm.background-service.plist").write_text("stale axiom BS")
        (launch_agents_dir / "com.domain-consumer.background-service.plist").write_text("current consumer BS")

        # Patch get_branding at SOURCE so the lazy import resolves to it
        monkeypatch.setattr(
            "axiom.infra.branding.get_branding",
            lambda: BrandingConfig(
                cli_name="neut",
                product_name="Domain Consumer",
                package_name="domain-consumer",
            ),
        )
        monkeypatch.setattr(
            "axiom.infra.branding.discover_portfolio_members",
            lambda: [
                PortfolioMember(
                    package_name="axiom-os-lm",
                    product_name="Axiom",
                    wrapper_binary="Axiom-Background-Service",
                ),
                PortfolioMember(
                    package_name="domain-consumer",
                    product_name="Domain Consumer",
                    wrapper_binary="DomainConsumer-Background-Service",
                ),
            ],
        )

        with patch.object(pathlib.Path, "home", return_value=tmp_path):
            with patch("subprocess.run"):
                results = agents_cli._cleanup_legacy_launchd()

        # Stale Axiom BS plist removed (cross-brand cleanup)
        assert not (launch_agents_dir / "com.axiom-os-lm.background-service.plist").exists()
        # Current consumer BS plist preserved
        assert (launch_agents_dir / "com.domain-consumer.background-service.plist").exists()
        # One cleanup result for the cross-brand BS plist
        cleanup_names = [r.agent_name for r in results if r.agent_name.startswith("<legacy-cleanup:")]
        assert "<legacy-cleanup:com.axiom-os-lm.background-service>" in cleanup_names
