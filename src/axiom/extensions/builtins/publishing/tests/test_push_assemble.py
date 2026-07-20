# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for neut pub push and neut pub assemble commands.

Tests:
  - _find_compile_manifest() detects .compile.yaml in directory and parent
  - _assemble_from_manifest() concatenates sources in order
  - cmd_assemble() writes assembled file and exits cleanly
  - cmd_push() with no .compile.yaml delegates directly to engine.publish
  - cmd_push() with .compile.yaml assembles transparently then publishes
  - cmd_push() cleans up temp assembled file even on publish failure
  - Parser: push and assemble subcommands are registered and parse correctly
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

COMPILE_YAML = textwrap.dedent("""\
    output: my-proposal
    title: "Test Multi-Section Proposal"
    sources:
      - 00-intro.md
      - 01-body.md
""")


def _write_compile_dir(tmp_path: Path) -> Path:
    """Create a directory with a .compile.yaml and two source files."""
    manifest = tmp_path / ".compile.yaml"
    manifest.write_text(COMPILE_YAML)
    (tmp_path / "00-intro.md").write_text("# Introduction\n\nThis is the intro.\n")
    (tmp_path / "01-body.md").write_text("# Body\n\nThis is the body.\n")
    return manifest


# ---------------------------------------------------------------------------
# _find_compile_manifest
# ---------------------------------------------------------------------------

def test_find_manifest_in_directory(tmp_path):
    from axiom.extensions.builtins.publishing.cli import _find_compile_manifest

    manifest = _write_compile_dir(tmp_path)
    result = _find_compile_manifest(tmp_path)  # pass directory
    assert result == manifest


def test_find_manifest_from_file_in_same_dir(tmp_path):
    from axiom.extensions.builtins.publishing.cli import _find_compile_manifest

    _write_compile_dir(tmp_path)
    some_file = tmp_path / "00-intro.md"
    result = _find_compile_manifest(some_file)  # pass a file in same dir
    assert result == tmp_path / ".compile.yaml"


def test_find_manifest_returns_none_when_absent(tmp_path):
    from axiom.extensions.builtins.publishing.cli import _find_compile_manifest

    # No .compile.yaml at all
    single_doc = tmp_path / "doc.md"
    single_doc.write_text("# Just a doc\n")
    assert _find_compile_manifest(single_doc) is None


# ---------------------------------------------------------------------------
# _assemble_from_manifest
# ---------------------------------------------------------------------------

def test_assemble_concatenates_sources(tmp_path):
    from axiom.extensions.builtins.publishing.cli import _assemble_from_manifest

    manifest = _write_compile_dir(tmp_path)
    output = tmp_path / "assembled.md"
    result = _assemble_from_manifest(manifest, output)

    assert result == output
    content = output.read_text()
    assert "# Introduction" in content
    assert "# Body" in content
    assert content.index("Introduction") < content.index("Body")


def test_assemble_includes_title(tmp_path):
    from axiom.extensions.builtins.publishing.cli import _assemble_from_manifest

    manifest = _write_compile_dir(tmp_path)
    output = tmp_path / "out.md"
    _assemble_from_manifest(manifest, output)

    content = output.read_text()
    assert "Test Multi-Section Proposal" in content


def test_assemble_raises_on_missing_source(tmp_path):
    from axiom.extensions.builtins.publishing.cli import _assemble_from_manifest

    manifest = tmp_path / ".compile.yaml"
    manifest.write_text("output: x\ntitle: X\nsources:\n  - missing.md\n")
    output = tmp_path / "out.md"

    with pytest.raises(FileNotFoundError, match="missing.md"):
        _assemble_from_manifest(manifest, output)


def test_assemble_default_output_name(tmp_path):
    from axiom.extensions.builtins.publishing.cli import _assemble_from_manifest

    manifest = _write_compile_dir(tmp_path)
    result = _assemble_from_manifest(manifest)  # no output_path

    assert result.exists()
    assert "my-proposal" in result.name or result.suffix == ".md"


# ---------------------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------------------

def test_push_parser_registered():
    from axiom.extensions.builtins.publishing.cli import get_parser

    parser = get_parser()
    args = parser.parse_args(["push", "docs/foo.md"])
    assert args.command == "push"
    assert args.path == "docs/foo.md"
    assert args.draft is False
    assert args.force is False


def test_push_parser_draft_flag():
    from axiom.extensions.builtins.publishing.cli import get_parser

    parser = get_parser()
    args = parser.parse_args(["push", "docs/foo.md", "--draft"])
    assert args.draft is True


def test_assemble_parser_registered():
    from axiom.extensions.builtins.publishing.cli import get_parser

    parser = get_parser()
    args = parser.parse_args(["assemble", "path/to/.compile.yaml"])
    assert args.command == "assemble"
    assert args.manifest == "path/to/.compile.yaml"
    assert args.output is None


def test_assemble_parser_output_flag():
    from axiom.extensions.builtins.publishing.cli import get_parser

    parser = get_parser()
    args = parser.parse_args(["assemble", ".compile.yaml", "--output", "out.md"])
    assert args.output == "out.md"


# ---------------------------------------------------------------------------
# cmd_assemble
# ---------------------------------------------------------------------------

def test_cmd_assemble_writes_file(tmp_path, capsys):
    from axiom.extensions.builtins.publishing.cli import cmd_assemble

    manifest = _write_compile_dir(tmp_path)
    output = tmp_path / "result.md"

    args = MagicMock()
    args.manifest = str(manifest)
    args.output = str(output)

    cmd_assemble(args)

    out = capsys.readouterr().out
    assert "Assembled" in out
    assert output.exists()
    assert "Introduction" in output.read_text()


def test_cmd_assemble_missing_manifest_exits(tmp_path):
    from axiom.extensions.builtins.publishing.cli import cmd_assemble

    args = MagicMock()
    args.manifest = str(tmp_path / "nonexistent.yaml")
    args.output = None

    with pytest.raises(SystemExit) as exc:
        cmd_assemble(args)
    assert exc.value.code != 0


# ---------------------------------------------------------------------------
# cmd_push — no manifest (simple single-file publish)
# ---------------------------------------------------------------------------

def test_cmd_push_single_file_no_manifest(tmp_path):
    from axiom.extensions.builtins.publishing.cli import cmd_push

    doc = tmp_path / "spec.md"
    doc.write_text("# Spec\n\nContent.\n")

    args = MagicMock()
    args.path = str(doc)
    args.draft = False
    args.endpoint = "local"
    args.force = False
    args.all = False
    args.headed = False
    args.command = "push"

    mock_engine = MagicMock()
    mock_engine.publish.return_value = {"version": "v1.0.0", "storage": "local", "url": str(tmp_path)}

    with patch("axiom.extensions.builtins.publishing.engine.PublisherEngine", return_value=mock_engine):
        cmd_push(args)

    mock_engine.publish.assert_called_once()
    call_kwargs = mock_engine.publish.call_args
    published_path = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("source")
    assert published_path == doc


# ---------------------------------------------------------------------------
# cmd_push — with .compile.yaml (transparent assembly)
# ---------------------------------------------------------------------------

def test_cmd_push_with_manifest_assembles_transparently(tmp_path, capsys):
    from axiom.extensions.builtins.publishing.cli import cmd_push

    _write_compile_dir(tmp_path)

    args = MagicMock()
    args.path = str(tmp_path)  # pass directory — manifest auto-detected
    args.draft = False
    args.endpoint = "local"
    args.force = False
    args.all = False
    args.headed = False

    mock_engine = MagicMock()
    mock_engine.publish.return_value = {"version": "v1.0.0", "storage": "local", "url": "/tmp/x"}

    published_paths = []

    def capture_publish(source, **kwargs):
        published_paths.append(source)
        return {"version": "v1.0.0", "storage": "local", "url": "/tmp/x"}

    mock_engine.publish.side_effect = capture_publish

    with patch("axiom.extensions.builtins.publishing.engine.PublisherEngine", return_value=mock_engine):
        cmd_push(args)

    # Should have been called with an assembled file
    assert len(published_paths) == 1
    published_file = published_paths[0]
    # The assembled temp file should be cleaned up after push
    assert not published_file.exists(), "Temp assembled file should be cleaned up"

    out = capsys.readouterr().out
    assert "Assembl" in out  # "Assembling" message printed


def test_cmd_push_cleans_up_temp_on_publish_failure(tmp_path):
    from axiom.extensions.builtins.publishing.cli import cmd_push

    _write_compile_dir(tmp_path)

    args = MagicMock()
    args.path = str(tmp_path)
    args.draft = False
    args.endpoint = None
    args.force = False
    args.all = False
    args.headed = False

    assembled_files = []

    mock_engine = MagicMock()

    def capture_and_fail(source, **kwargs):
        assembled_files.append(source)
        raise RuntimeError("Storage unavailable")

    mock_engine.publish.side_effect = capture_and_fail

    with patch("axiom.extensions.builtins.publishing.engine.PublisherEngine", return_value=mock_engine):
        with pytest.raises(RuntimeError):
            cmd_push(args)

    # Temp file must still be cleaned up
    if assembled_files:
        assert not assembled_files[0].exists(), "Temp file should be cleaned up even on failure"
