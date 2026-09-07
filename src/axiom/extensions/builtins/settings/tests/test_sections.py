# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Test-first contract tests for spec-settings (extension-registered sections).

Tests are derived from spec-settings.md §6 "Test plan for the
implementation PR." Each test maps to one row in that table.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestDataClassShapes:
    """Per spec §4.1 manifest fields + §2.2 SectionView dataclass."""

    def test_settings_section_def_has_manifest_fields(self):
        from axiom.infra.settings_sections import SettingsSectionDef

        d = SettingsSectionDef(
            name="example",
            display_name="Example",
            description="An example section",
            entry="some.module:get_section",
        )
        assert d.name == "example"
        assert d.display_name == "Example"
        assert d.entry == "some.module:get_section"
        assert d.wizard is None
        assert d.schema is None
        assert d.intent_groups == ()

    def test_section_view_has_runtime_fields(self):
        from axiom.infra.settings_sections import SectionView

        v = SectionView(
            name="example",
            display_name="Example",
            description="An example section",
            values={"key": "value"},
            summary="1 key set",
            is_active=True,
        )
        assert v.name == "example"
        assert v.values == {"key": "value"}
        assert v.is_active is True
        assert v.wizard is None


class TestParseSettingsSections:
    """`parse_settings_sections(manifest_dict)` extracts [[settings.sections]]."""

    def test_parses_a_single_section(self):
        from axiom.infra.settings_sections import parse_settings_sections

        manifest = {
            "extension": {"name": "ext"},
            "settings": {
                "sections": [
                    {
                        "name": "routing",
                        "display_name": "LLM routing",
                        "description": "Provider preferences",
                        "entry": "ext.settings:get_section",
                    }
                ]
            },
        }
        sections = parse_settings_sections(manifest)
        assert len(sections) == 1
        assert sections[0].name == "routing"
        assert sections[0].entry == "ext.settings:get_section"

    def test_parses_optional_wizard_and_schema(self):
        from axiom.infra.settings_sections import parse_settings_sections

        manifest = {
            "settings": {
                "sections": [
                    {
                        "name": "routing",
                        "display_name": "Routing",
                        "description": "x",
                        "entry": "ext:get",
                        "wizard": "ext:run_wizard",
                        "schema": "ext:SCHEMA",
                        "intent_groups": ["start", "operate"],
                    }
                ]
            }
        }
        sections = parse_settings_sections(manifest)
        assert sections[0].wizard == "ext:run_wizard"
        assert sections[0].schema == "ext:SCHEMA"
        assert sections[0].intent_groups == ("start", "operate")

    def test_returns_empty_list_when_no_sections_block(self):
        from axiom.infra.settings_sections import parse_settings_sections

        assert parse_settings_sections({"extension": {"name": "ext"}}) == []
        assert parse_settings_sections({"settings": {}}) == []
        assert parse_settings_sections({}) == []


class TestDiscoverSettingsSections:
    """`discover_settings_sections()` enumerates from all installed extensions."""

    def test_returns_sections_from_each_enabled_extension(self):
        from axiom.infra.settings_sections import (
            SettingsSectionDef,
            discover_settings_sections,
        )

        ext_a = MagicMock(enabled=True)
        ext_a.settings_sections = [
            SettingsSectionDef(name="a", display_name="A", description="", entry="a:f")
        ]
        ext_b = MagicMock(enabled=True)
        ext_b.settings_sections = [
            SettingsSectionDef(name="b", display_name="B", description="", entry="b:f")
        ]
        disabled = MagicMock(enabled=False)
        disabled.settings_sections = [
            SettingsSectionDef(name="x", display_name="X", description="", entry="x:f")
        ]

        with patch(
            "axiom.infra.settings_sections.discover_extensions",
            return_value=[ext_a, disabled, ext_b],
        ):
            sections = discover_settings_sections()

        names = sorted(s.name for s in sections)
        assert names == ["a", "b"]

    def test_first_definition_wins_on_name_conflict(self):
        from axiom.infra.settings_sections import (
            SettingsSectionDef,
            discover_settings_sections,
        )

        local = SettingsSectionDef(name="routing", display_name="Local", description="", entry="local:f")
        builtin = SettingsSectionDef(name="routing", display_name="Builtin", description="", entry="builtin:f")
        ext_local = MagicMock(enabled=True)
        ext_local.settings_sections = [local]
        ext_builtin = MagicMock(enabled=True)
        ext_builtin.settings_sections = [builtin]

        with patch(
            "axiom.infra.settings_sections.discover_extensions",
            return_value=[ext_local, ext_builtin],
        ):
            sections = discover_settings_sections()

        assert len(sections) == 1
        assert sections[0].display_name == "Local"


class TestLoadSectionView:
    """`load_section_view(section_def)` invokes the entry callable."""

    def test_invokes_entry_callable_and_returns_view(self):
        from axiom.infra.settings_sections import (
            SectionView,
            SettingsSectionDef,
            load_section_view,
        )

        section_def = SettingsSectionDef(
            name="example",
            display_name="Example",
            description="x",
            entry="fakemod:get_section",
        )
        expected = SectionView(
            name="example",
            display_name="Example",
            description="x",
            values={"k": "v"},
            summary="1 key set",
            is_active=True,
        )

        with patch(
            "axiom.infra.settings_sections._resolve_entry",
            return_value=lambda: expected,
        ):
            view = load_section_view(section_def)

        assert view is expected

    def test_returns_none_when_entry_raises(self):
        from axiom.infra.settings_sections import (
            SettingsSectionDef,
            load_section_view,
        )

        section_def = SettingsSectionDef(
            name="broken",
            display_name="Broken",
            description="",
            entry="x:y",
        )

        def broken_entry():
            raise RuntimeError("boom")

        with patch(
            "axiom.infra.settings_sections._resolve_entry",
            return_value=broken_entry,
        ):
            view = load_section_view(section_def)

        assert view is None


class TestSettingsListCLI:
    """`axi settings` (no args) lists only active sections per spec §2.3."""

    def test_lists_only_active_sections(self, capsys):
        from axiom.extensions.builtins.settings.cli import cmd_settings_list
        from axiom.infra.settings_sections import SectionView

        active = SectionView(
            name="routing", display_name="Routing", description="",
            values={"k": "v"}, summary="1 key set", is_active=True,
        )
        inactive = SectionView(
            name="unused", display_name="Unused", description="",
            values={}, summary="(no values)", is_active=False,
        )
        cmd_settings_list(views=[active, inactive])
        out = capsys.readouterr().out

        assert "routing" in out
        assert "unused" not in out

    def test_all_flag_includes_inactive(self, capsys):
        from axiom.extensions.builtins.settings.cli import cmd_settings_list
        from axiom.infra.settings_sections import SectionView

        inactive = SectionView(
            name="unused", display_name="Unused", description="",
            values={}, summary="(no values)", is_active=False,
        )
        cmd_settings_list(views=[inactive], show_all=True)
        out = capsys.readouterr().out
        assert "unused" in out

    def test_rich_table_shape(self, capsys):
        """Beautified output: a real table with header + summary column."""
        from axiom.extensions.builtins.settings.cli import cmd_settings_list
        from axiom.infra.settings_sections import SectionView

        cmd_settings_list(views=[
            SectionView(name="routing", display_name="Routing",
                        description="", values={"k": "v"},
                        summary="1 key set", is_active=True),
            SectionView(name="federation", display_name="Federation",
                        description="", values={"peers": 3},
                        summary="3 peers reachable", is_active=True),
        ])
        out = capsys.readouterr().out
        # Header row + a horizontal separator are the table-shape signal.
        assert "Section" in out and "Summary" in out
        # Both sections rendered
        assert "routing" in out
        assert "federation" in out
        # Drill-in hint and --all hint remain visible
        assert "drill in" in out.lower()
        assert "--all" in out

    def test_all_flag_shows_active_column(self, capsys):
        """With --all, the table marks active vs inactive in a column."""
        from axiom.extensions.builtins.settings.cli import cmd_settings_list
        from axiom.infra.settings_sections import SectionView

        cmd_settings_list(views=[
            SectionView(name="routing", display_name="Routing",
                        description="", values={"k": "v"},
                        summary="active", is_active=True),
            SectionView(name="unused", display_name="Unused",
                        description="", values={}, summary="empty",
                        is_active=False),
        ], show_all=True)
        out = capsys.readouterr().out
        assert "routing" in out
        assert "unused" in out
        # Active-state indicator becomes its own column under --all
        assert "Active" in out or "Status" in out


class TestSettingsViewCLI:
    """`axi settings <section>` shows that section's values."""

    def test_shows_values_in_order(self, capsys):
        from axiom.extensions.builtins.settings.cli import cmd_settings_view
        from axiom.infra.settings_sections import SectionView

        view = SectionView(
            name="routing", display_name="Routing", description="",
            values={"alpha": "1", "beta": "2"}, summary="2 keys set", is_active=True,
        )
        cmd_settings_view(view)
        out = capsys.readouterr().out
        assert "alpha" in out
        assert "beta" in out
        assert "[routing]" in out


class TestSettingsSetupCLI:
    """`axi settings setup` invokes per-section wizards in order."""

    def test_invokes_each_wizard_in_order(self):
        from axiom.extensions.builtins.settings.cli import cmd_settings_setup
        from axiom.infra.settings_sections import (
            SectionView,
            SettingsSectionDef,
        )

        call_order: list[str] = []

        def make_wizard(name):
            def wiz():
                call_order.append(name)
                return SectionView(
                    name=name, display_name=name, description="",
                    values={"k": "v"}, summary="set", is_active=True,
                )
            return wiz

        defs = [
            SettingsSectionDef(name="a", display_name="A", description="", entry="x:f", wizard="x:wa"),
            SettingsSectionDef(name="b", display_name="B", description="", entry="x:f", wizard="x:wb"),
        ]

        def fake_resolve(spec):
            if spec.endswith("wa"):
                return make_wizard("a")
            if spec.endswith("wb"):
                return make_wizard("b")
            return None

        with patch(
            "axiom.infra.settings_sections._resolve_entry",
            side_effect=fake_resolve,
        ):
            cmd_settings_setup(defs)

        assert call_order == ["a", "b"]


class TestAxiConfigAlias:
    """`axi config` aliases to `axi settings setup` with a banner (§3.6)."""

    def test_axi_config_runs_setup(self):
        from axiom.setup import cli as setup_cli

        with patch.object(setup_cli, "run_settings_setup") as run_setup:
            setup_cli.alias_to_settings_setup()
        run_setup.assert_called_once()

    def test_axi_config_emits_banner(self, capsys, tmp_path, monkeypatch):
        from axiom.setup import cli as setup_cli

        monkeypatch.setattr(setup_cli, "_alias_banner_marker", lambda: tmp_path / "marker")

        with patch.object(setup_cli, "run_settings_setup"):
            setup_cli.alias_to_settings_setup()

        out = capsys.readouterr().out
        assert "axi settings setup" in out

    def test_axi_config_banner_shown_once_per_session(self, capsys, tmp_path, monkeypatch):
        from axiom.setup import cli as setup_cli

        monkeypatch.setattr(setup_cli, "_alias_banner_marker", lambda: tmp_path / "marker")

        with patch.object(setup_cli, "run_settings_setup"):
            setup_cli.alias_to_settings_setup()
            setup_cli.alias_to_settings_setup()

        out = capsys.readouterr().out
        assert out.count("axi settings setup") == 1


class TestSettingsCLIDispatch:
    """End-to-end CLI dispatch — argv → cmd_* invocation."""

    def test_setup_subcommand_calls_cmd_settings_setup(self, monkeypatch):
        from axiom.extensions.builtins.settings import cli as settings_cli

        monkeypatch.setattr("sys.argv", ["axi-settings", "setup"])
        called = MagicMock(return_value=0)
        monkeypatch.setattr(settings_cli, "cmd_settings_setup", called)
        monkeypatch.setattr(
            settings_cli,
            "discover_settings_sections_for_main",
            lambda: ["fake-def"],
        )

        settings_cli.main()
        called.assert_called_once_with(["fake-def"])

    def test_no_args_with_sections_uses_unified_list(self, monkeypatch, capsys):
        from axiom.extensions.builtins.settings import cli as settings_cli
        from axiom.infra.settings_sections import SectionView

        monkeypatch.setattr("sys.argv", ["axi-settings"])
        view = SectionView(
            name="routing", display_name="Routing", description="",
            values={"k": "v"}, summary="1 key set", is_active=True,
        )
        monkeypatch.setattr(
            settings_cli,
            "load_section_views_for_main",
            lambda: [view],
        )
        settings_cli.main()
        out = capsys.readouterr().out
        assert "routing" in out
        assert "1 key set" in out

    def test_axi_config_main_invokes_alias(self, monkeypatch):
        from axiom.setup import cli as setup_cli

        monkeypatch.setattr("sys.argv", ["axi-config"])
        alias = MagicMock(return_value=0)
        monkeypatch.setattr(setup_cli, "alias_to_settings_setup", alias)

        setup_cli.main()
        alias.assert_called_once()
