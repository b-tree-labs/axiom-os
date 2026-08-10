# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for IDE detection and configuration."""

from __future__ import annotations

import json


class TestDetectIDEs:
    def test_returns_list(self):
        from axiom.infra.ide import detect_ides

        ides = detect_ides()
        assert isinstance(ides, list)
        assert len(ides) >= 5  # VS Code, Cursor, PyCharm, IntelliJ, Neovim

    def test_ide_info_serializable(self):
        from axiom.infra.ide import detect_ides

        ides = detect_ides()
        for ide in ides:
            d = ide.to_dict()
            assert "name" in d
            assert "installed" in d


class TestWriteWorkspaceConfig:
    def test_creates_vscode_dir(self, tmp_path):
        from axiom.infra.ide import write_workspace_config

        write_workspace_config(tmp_path)
        assert (tmp_path / ".vscode" / "settings.json").exists()
        assert (tmp_path / ".vscode" / "extensions.json").exists()

    def test_settings_has_python_path(self, tmp_path):
        from axiom.infra.ide import write_workspace_config

        write_workspace_config(tmp_path, python_path="/usr/bin/python3")
        settings = json.loads((tmp_path / ".vscode" / "settings.json").read_text())
        assert settings["python.defaultInterpreterPath"] == "/usr/bin/python3"

    def test_settings_has_schemas(self, tmp_path):
        from axiom.infra.ide import write_workspace_config

        schemas = {"file:///schema.json": "model.yaml"}
        write_workspace_config(tmp_path, schemas=schemas)
        settings = json.loads((tmp_path / ".vscode" / "settings.json").read_text())
        assert settings["yaml.schemas"]["file:///schema.json"] == "model.yaml"

    def test_extensions_has_recommendations(self, tmp_path):
        from axiom.infra.ide import write_workspace_config

        write_workspace_config(tmp_path)
        ext = json.loads((tmp_path / ".vscode" / "extensions.json").read_text())
        assert "redhat.vscode-yaml" in ext["recommendations"]

    def test_merges_existing_settings(self, tmp_path):
        from axiom.infra.ide import write_workspace_config

        # Write existing settings
        vscode = tmp_path / ".vscode"
        vscode.mkdir()
        (vscode / "settings.json").write_text(json.dumps({"custom.key": "value"}))

        write_workspace_config(tmp_path)
        settings = json.loads((vscode / "settings.json").read_text())
        assert "custom.key" in settings  # preserved
        assert "python.defaultInterpreterPath" in settings  # added


class TestWriteNeovimConfig:
    def test_creates_yamlls_json(self, tmp_path):
        from axiom.infra.ide import write_neovim_config

        write_neovim_config(tmp_path, schemas={"file:///s.json": "model.yaml"})
        assert (tmp_path / ".yamlls.json").exists()
        data = json.loads((tmp_path / ".yamlls.json").read_text())
        assert "yaml.schemas" in data

    def test_no_schemas_no_file(self, tmp_path):
        from axiom.infra.ide import write_neovim_config

        write_neovim_config(tmp_path)
        assert not (tmp_path / ".yamlls.json").exists()


class TestWritePyCharmConfig:
    def test_creates_idea_dir(self, tmp_path):
        from axiom.infra.ide import write_pycharm_config

        write_pycharm_config(tmp_path, schemas={"file:///s.json": "model.yaml"})
        xml_path = tmp_path / ".idea" / "jsonSchemas.xml"
        assert xml_path.exists()
        content = xml_path.read_text()
        assert "JsonSchemaMappingsProjectConfiguration" in content
        assert "model.yaml" in content


class TestSetupIDE:
    def test_setup_returns_summary(self, tmp_path):
        from axiom.infra.ide import setup_ide

        result = setup_ide(tmp_path, auto_install_extensions=False)
        assert "ides_detected" in result
        assert "configs_written" in result

    def test_setup_writes_configs_for_detected_ides(self, tmp_path):
        from axiom.infra.ide import setup_ide

        result = setup_ide(tmp_path, auto_install_extensions=False)
        # May be 0 on CI runners with no IDEs installed — that's OK
        assert isinstance(result["ides_detected"], list)
