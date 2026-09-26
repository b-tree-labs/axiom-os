# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Stranger-path regression tests for `axi config` (sandbox audit 2026-09-18).

Gap 1 (P0): bare `axi config` routed to the `settings setup` alias, which in a
bare install discovers zero section wizards and exits 0 having provisioned
nothing — while `axi chat`'s fix advice points back at `axi config`. The alias
must fall back to the full onboarding wizard when the section chain is empty.

Gap 2 (P0): the sub-4.7GB path (`--model small`, `--dry-run`) was hidden from
`axi config --help`, and the default 4.7GB download started behind a
default-Yes prompt with no size confirmation.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from axiom.infra.settings_sections import SettingsSectionDef
from axiom.setup import cli as setup_cli


def _section(name: str, wizard: str | None) -> SettingsSectionDef:
    return SettingsSectionDef(
        name=name,
        display_name=name,
        description="",
        entry=f"tests.fake:{name}_entry",
        wizard=wizard,
    )


class TestBareConfigFallsBackToWizard:
    """`axi config` in a bare install must run a real wizard, not a no-op."""

    def test_no_sections_at_all_runs_legacy_wizard(self):
        wizard = MagicMock()
        with (
            patch(
                "axiom.infra.settings_sections.discover_settings_sections",
                return_value=[],
            ),
            patch.object(setup_cli, "SetupWizard", return_value=wizard) as wiz_cls,
            patch(
                "axiom.extensions.builtins.settings.cli.cmd_settings_setup"
            ) as chain,
        ):
            rc = setup_cli.run_settings_setup()

        assert rc == 0
        wiz_cls.assert_called_once()
        wizard.run.assert_called_once()
        chain.assert_not_called()

    def test_sections_without_wizards_also_fall_back(self):
        """Sections that declare no wizard entry cannot onboard anyone."""
        wizard = MagicMock()
        with (
            patch(
                "axiom.infra.settings_sections.discover_settings_sections",
                return_value=[_section("routing", wizard=None)],
            ),
            patch.object(setup_cli, "SetupWizard", return_value=wizard),
            patch(
                "axiom.extensions.builtins.settings.cli.cmd_settings_setup"
            ) as chain,
        ):
            rc = setup_cli.run_settings_setup()

        assert rc == 0
        wizard.run.assert_called_once()
        chain.assert_not_called()

    def test_registered_wizards_still_use_section_chain(self):
        defs = [_section("routing", wizard="tests.fake:routing_wizard")]
        with (
            patch(
                "axiom.infra.settings_sections.discover_settings_sections",
                return_value=defs,
            ),
            patch.object(setup_cli, "SetupWizard") as wiz_cls,
            patch(
                "axiom.extensions.builtins.settings.cli.cmd_settings_setup",
                return_value=0,
            ) as chain,
        ):
            rc = setup_cli.run_settings_setup()

        assert rc == 0
        chain.assert_called_once_with(defs)
        wiz_cls.assert_not_called()

    def test_keyboard_interrupt_in_fallback_exits_130(self):
        wizard = MagicMock()
        wizard.run.side_effect = KeyboardInterrupt
        with (
            patch(
                "axiom.infra.settings_sections.discover_settings_sections",
                return_value=[],
            ),
            patch.object(setup_cli, "SetupWizard", return_value=wizard),
        ):
            rc = setup_cli.run_settings_setup()

        assert rc == 130


class TestConfigHelpAdvertisesSmallModelPath:
    """`axi config --help` must surface --model (with the small profile's
    size) and --dry-run — the audit found them hidden while the default
    profile is a 4.7GB pull."""

    def _help_text(self, capsys) -> str:
        setup_cli._print_help()
        return capsys.readouterr().out

    def test_help_mentions_model_flag(self, capsys):
        out = self._help_text(capsys)
        assert "--model" in out

    def test_help_mentions_small_profile_and_size(self, capsys):
        out = self._help_text(capsys)
        assert "small" in out
        assert "1.6" in out

    def test_help_mentions_default_size(self, capsys):
        out = self._help_text(capsys)
        assert "4.7" in out

    def test_help_mentions_dry_run(self, capsys):
        out = self._help_text(capsys)
        assert "--dry-run" in out
