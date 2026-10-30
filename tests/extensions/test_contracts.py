# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for extension manifest parsing and validation."""

from pathlib import Path

import pytest

from axiom.extensions.contracts import (
    Extension,
    _parse_skill_description,
    _scan_skills,
    parse_manifest,
    validate_extension,
)


@pytest.fixture
def ext_dir(tmp_path):
    """Create a minimal valid extension directory."""
    d = tmp_path / "test-ext"
    d.mkdir()
    (d / "tools_ext").mkdir()
    (d / "tools_ext" / "__init__.py").write_text("")
    (d / "tools_ext" / "my_tool.py").write_text(
        "TOOLS = []\ndef execute(name, params): return {}"
    )
    (d / "skills" / "my-skill").mkdir(parents=True)
    (d / "skills" / "my-skill" / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: A test skill\n---\n# My Skill\nDoes things.\n"
    )
    (d / "cli").mkdir()
    (d / "cli" / "myverb.py").write_text("def main(): pass")
    (d / "providers").mkdir()
    (d / "providers" / "my_prov.py").write_text("class MyProv: pass")
    (d / "extractors").mkdir()
    (d / "extractors" / "my_ext.py").write_text("class MyExt: pass")
    return d


def _write_manifest(ext_dir: Path, content: str) -> Path:
    manifest = ext_dir / "axiom-extension.toml"
    manifest.write_text(content, encoding="utf-8")
    return manifest


class TestParseManifest:
    def test_minimal_manifest(self, ext_dir):
        _write_manifest(ext_dir, '[extension]\nname = "test-ext"\n')
        ext = parse_manifest(ext_dir / "axiom-extension.toml")
        assert ext.name == "test-ext"
        assert ext.version == "0.1.0"
        assert ext.root == ext_dir

    def test_full_manifest(self, ext_dir):
        _write_manifest(
            ext_dir,
            """\
[extension]
name = "test-ext"
version = "1.2.3"
description = "A full extension"
author = "Test Author"

[[extension.provides]]
kind = "cmd"
noun = "myverb"
entry = "cli.myverb:main"
description = "My custom verb"

[chat_tools]
module = "tools_ext"

[skills]
dir = "skills"

[[providers]]
type = "generation"
name = "pptx"
module = "providers.my_prov"

[[extractors]]
name = "my_ext"
module = "extractors.my_ext"
file_patterns = ["*.csv"]

[mcp_servers.test_server]
type = "stdio"
command = "python"
args = ["-m", "test_mcp"]
env = { KEY = "value" }
""",
        )
        ext = parse_manifest(ext_dir / "axiom-extension.toml")
        assert ext.name == "test-ext"
        assert ext.version == "1.2.3"
        assert ext.description == "A full extension"
        assert ext.author == "Test Author"
        assert ext.chat_tools_module == "tools_ext"
        assert len(ext.cli_commands) == 1
        assert ext.cli_commands[0].noun == "myverb"
        assert ext.cli_commands[0].function == "main"
        assert len(ext.providers) == 1
        assert ext.providers[0].type == "generation"
        assert ext.providers[0].name == "pptx"
        assert len(ext.extractors) == 1
        assert ext.extractors[0].file_patterns == ["*.csv"]
        assert "test_server" in ext.mcp_servers
        assert ext.mcp_servers["test_server"].command == "python"

    def test_cmd_requires_field(self, ext_dir):
        # ADR-047: a cmd may declare capability requirements.
        _write_manifest(
            ext_dir,
            """\
[extension]
name = "test-ext"
version = "1.0.0"
description = "x"

[[extension.provides]]
kind = "cmd"
noun = "myverb"
entry = "cli.myverb:main"
requires = ["git", "gh"]
""",
        )
        ext = parse_manifest(ext_dir / "axiom-extension.toml")
        assert ext.cli_commands[0].requires == ["git", "gh"]

    def test_missing_name_raises(self, ext_dir):
        _write_manifest(ext_dir, "[extension]\nversion = '1.0'\n")
        with pytest.raises(ValueError, match="Missing.*name"):
            parse_manifest(ext_dir / "axiom-extension.toml")

    def test_missing_manifest_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            parse_manifest(tmp_path / "nonexistent" / "axiom-extension.toml")

    def test_skills_scanned_automatically(self, ext_dir):
        _write_manifest(ext_dir, '[extension]\nname = "test-ext"\n[skills]\ndir = "skills"\n')
        ext = parse_manifest(ext_dir / "axiom-extension.toml")
        assert len(ext.skills) == 1
        assert ext.skills[0].name == "my-skill"
        assert ext.skills[0].description == "A test skill"

    def test_capabilities_list(self, ext_dir):
        _write_manifest(
            ext_dir,
            """\
[extension]
name = "test-ext"

[[extension.provides]]
kind = "cmd"
noun = "myverb"
entry = "cli.myverb:main"

[chat_tools]
module = "tools_ext"

[skills]
dir = "skills"
""",
        )
        ext = parse_manifest(ext_dir / "axiom-extension.toml")
        caps = ext.capabilities
        assert "chat tools" in caps
        assert any("skill" in c for c in caps)
        assert any("CLI" in c for c in caps)


class TestValidateExtension:
    def test_valid_extension(self, ext_dir):
        _write_manifest(
            ext_dir,
            """\
[extension]
name = "test-ext"

[[extension.provides]]
kind = "cmd"
noun = "myverb"
entry = "cli.myverb:main"

[chat_tools]
module = "tools_ext"

[[providers]]
type = "generation"
name = "pptx"
module = "providers.my_prov"

[[extractors]]
name = "my_ext"
module = "extractors.my_ext"
""",
        )
        ext = parse_manifest(ext_dir / "axiom-extension.toml")
        issues = validate_extension(ext)
        assert issues == []

    def test_missing_tools_dir(self, ext_dir):
        import shutil

        shutil.rmtree(ext_dir / "tools_ext")
        _write_manifest(
            ext_dir,
            '[extension]\nname = "test-ext"\n[chat_tools]\nmodule = "tools_ext"\n',
        )
        ext = parse_manifest(ext_dir / "axiom-extension.toml")
        issues = validate_extension(ext)
        assert any("tools_ext" in i for i in issues)

    def test_missing_cli_module(self, ext_dir):
        _write_manifest(
            ext_dir,
            '[extension]\nname = "test-ext"\n\n[[extension.provides]]\nkind = "cmd"\nnoun = "foo"\nentry = "cli.nonexistent:main"\n',
        )
        ext = parse_manifest(ext_dir / "axiom-extension.toml")
        issues = validate_extension(ext)
        assert any("nonexistent" in i for i in issues)

    def test_missing_extension_root(self, tmp_path):
        ext = Extension(
            name="ghost",
            version="0.1.0",
            description="",
            author="",
            root=tmp_path / "nonexistent" / "ghost",
        )
        issues = validate_extension(ext)
        assert any("not found" in i for i in issues)


class TestScanSkills:
    def test_scan_finds_skill_md(self, tmp_path):
        skill_dir = tmp_path / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# My Skill\nDoes things.\n")
        skills = _scan_skills(tmp_path / "skills")
        assert len(skills) == 1
        assert skills[0].name == "my-skill"

    def test_scan_empty_dir(self, tmp_path):
        (tmp_path / "skills").mkdir()
        skills = _scan_skills(tmp_path / "skills")
        assert skills == []

    def test_scan_nested_skills(self, tmp_path):
        for name in ["alpha", "beta"]:
            d = tmp_path / "skills" / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"# {name}\nSkill {name}.\n")
        skills = _scan_skills(tmp_path / "skills")
        assert len(skills) == 2
        assert skills[0].name == "alpha"
        assert skills[1].name == "beta"


class TestParseSkillDescription:
    def test_frontmatter_description(self, tmp_path):
        f = tmp_path / "SKILL.md"
        f.write_text('---\nname: test\ndescription: "A great skill"\n---\n# Test\n')
        assert _parse_skill_description(f) == "A great skill"

    def test_first_paragraph_fallback(self, tmp_path):
        f = tmp_path / "SKILL.md"
        f.write_text("# Test\n\nThis is the first paragraph.\n")
        assert _parse_skill_description(f) == "This is the first paragraph."

    def test_empty_file(self, tmp_path):
        f = tmp_path / "SKILL.md"
        f.write_text("")
        assert _parse_skill_description(f) == ""

    def test_missing_file(self, tmp_path):
        f = tmp_path / "nonexistent.md"
        assert _parse_skill_description(f) == ""
