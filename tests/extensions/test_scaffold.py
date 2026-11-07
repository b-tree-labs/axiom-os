# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for extension scaffolding."""


import pytest

from axiom.extensions.contracts import parse_manifest
from axiom.extensions.scaffold import scaffold_extension


class TestScaffoldExtension:
    def test_creates_directory_structure(self, tmp_path):
        ext_dir = scaffold_extension("my-ext", base_dir=tmp_path)
        assert ext_dir.is_dir()
        assert (ext_dir / "axiom-extension.toml").exists()
        assert (ext_dir / "tools_ext" / "__init__.py").exists()
        assert (ext_dir / "tools_ext" / "data_query.py").exists()
        assert (ext_dir / "skills" / "weekly-slides" / "SKILL.md").exists()
        assert (ext_dir / "providers" / "__init__.py").exists()
        assert (ext_dir / "providers" / "pptx_generation.py").exists()
        assert (ext_dir / "cli" / "__init__.py").exists()
        assert (ext_dir / "cli" / "logs.py").exists()
        assert (ext_dir / "extractors" / "__init__.py").exists()
        assert (ext_dir / "extractors" / "data_log.py").exists()

    def test_manifest_is_valid(self, tmp_path):
        ext_dir = scaffold_extension("my-ext", base_dir=tmp_path, author="Test User")
        ext = parse_manifest(ext_dir / "axiom-extension.toml")
        assert ext.name == "my-ext"
        assert ext.author == "Test User"
        assert ext.chat_tools_module == "tools_ext"
        assert len(ext.cli_commands) >= 1
        assert len(ext.providers) >= 1
        assert len(ext.extractors) >= 1

    def test_skill_md_is_valid(self, tmp_path):
        ext_dir = scaffold_extension("my-ext", base_dir=tmp_path)
        skill_md = ext_dir / "skills" / "weekly-slides" / "SKILL.md"
        content = skill_md.read_text()
        assert "weekly-slides" in content
        assert "---" in content  # Has frontmatter

    def test_chat_tool_is_importable(self, tmp_path):
        ext_dir = scaffold_extension("my-ext", base_dir=tmp_path)
        tool_file = ext_dir / "tools_ext" / "data_query.py"
        assert tool_file.exists()
        # Verify it's valid Python
        content = tool_file.read_text()
        compile(content, str(tool_file), "exec")

    def test_provider_is_importable(self, tmp_path):
        ext_dir = scaffold_extension("my-ext", base_dir=tmp_path)
        prov_file = ext_dir / "providers" / "pptx_generation.py"
        content = prov_file.read_text()
        compile(content, str(prov_file), "exec")

    def test_cli_is_importable(self, tmp_path):
        ext_dir = scaffold_extension("my-ext", base_dir=tmp_path)
        cli_file = ext_dir / "cli" / "logs.py"
        content = cli_file.read_text()
        compile(content, str(cli_file), "exec")

    def test_extractor_is_importable(self, tmp_path):
        ext_dir = scaffold_extension("my-ext", base_dir=tmp_path)
        ext_file = ext_dir / "extractors" / "data_log.py"
        content = ext_file.read_text()
        compile(content, str(ext_file), "exec")

    def test_duplicate_raises(self, tmp_path):
        scaffold_extension("my-ext", base_dir=tmp_path)
        with pytest.raises(FileExistsError):
            scaffold_extension("my-ext", base_dir=tmp_path)

    def test_custom_description(self, tmp_path):
        ext_dir = scaffold_extension(
            "my-ext", base_dir=tmp_path, description="Custom desc"
        )
        ext = parse_manifest(ext_dir / "axiom-extension.toml")
        assert ext.description == "Custom desc"

    def test_default_description(self, tmp_path):
        ext_dir = scaffold_extension("my-ext", base_dir=tmp_path)
        ext = parse_manifest(ext_dir / "axiom-extension.toml")
        assert "my-ext" in ext.description

    def test_creates_parent_dirs(self, tmp_path):
        base = tmp_path / "deep" / "nested" / "path"
        ext_dir = scaffold_extension("my-ext", base_dir=base)
        assert ext_dir.is_dir()
