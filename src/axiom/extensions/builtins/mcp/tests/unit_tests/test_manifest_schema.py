# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Manifest schema parsing + lint tests for ``[extension.mcp]``.

Spec: ``docs/specs/spec-builtin-mcp-server.md`` §7.

The schema parser turns a TOML manifest into ``MCPExtensionConfig``;
``axi ext lint`` (via ``lint_mcp_block``) flags missing/invalid blocks.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from axiom.extensions.builtins.mcp.manifest_schema import (
    LintError,
    MCPExtensionConfig,
    lint_mcp_block,
    parse_mcp_block,
)

# ---------------------------------------------------------------------------
# parse_mcp_block — parses + applies defaults
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "axiom-extension.toml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_parse_minimal_block(tmp_path: Path):
    """A minimal opt-in block parses with all defaults applied."""
    path = _write_manifest(
        tmp_path,
        '''
        [extension]
        name = "demo"
        version = "0.0.1"

        [extension.mcp]
        enabled = true
        ''',
    )
    cfg = parse_mcp_block(path)
    assert cfg is not None
    assert isinstance(cfg, MCPExtensionConfig)
    assert cfg.enabled is True
    assert cfg.prefix == "axiom_demo"
    assert cfg.visibility == "public"
    assert cfg.auth == "local_stdio"
    assert cfg.tools == []
    assert cfg.resources == []
    assert cfg.prompts == []


def test_parse_explicit_optout(tmp_path: Path):
    """``enabled = false`` parses to a config flagged as opted-out."""
    path = _write_manifest(
        tmp_path,
        '''
        [extension]
        name = "demo"
        version = "0.0.1"

        [extension.mcp]
        enabled = false
        ''',
    )
    cfg = parse_mcp_block(path)
    assert cfg is not None
    assert cfg.enabled is False


def test_parse_returns_none_when_no_block(tmp_path: Path):
    """No ``[extension.mcp]`` block at all -> parser returns ``None``."""
    path = _write_manifest(
        tmp_path,
        '''
        [extension]
        name = "demo"
        version = "0.0.1"
        ''',
    )
    cfg = parse_mcp_block(path)
    assert cfg is None


def test_parse_per_tool_overrides(tmp_path: Path):
    """``[[extension.mcp.tool]]`` overrides apply (mcp_name, description, etc.)."""
    path = _write_manifest(
        tmp_path,
        '''
        [extension]
        name = "demo"
        version = "0.0.1"

        [extension.mcp]
        enabled = true
        prefix = "demo"
        description = "demo surface"

        [[extension.provides]]
        kind = "tool"
        name = "ping"
        description = "ping"

        [[extension.mcp.tool]]
        name = "ping"
        mcp_name = "demo__pong"
        description_override = "Returns pong instead."
        side_effects = "none"
        ''',
    )
    cfg = parse_mcp_block(path)
    assert cfg is not None
    assert len(cfg.tools) == 1
    tool = cfg.tools[0]
    assert tool.name == "ping"
    assert tool.mcp_name == "demo__pong"
    assert tool.description_override == "Returns pong instead."
    assert tool.side_effects == "none"


def test_parse_resource_block(tmp_path: Path):
    """``[[extension.mcp.resource]]`` parses with required fields."""
    path = _write_manifest(
        tmp_path,
        '''
        [extension]
        name = "demo"
        version = "0.0.1"

        [extension.mcp]
        enabled = true

        [[extension.mcp.resource]]
        name = "card"
        uri_template = "axiom://demo/{id}"
        entry = "demo.module:read"
        mime_type = "application/json"
        ''',
    )
    cfg = parse_mcp_block(path)
    assert cfg is not None
    assert len(cfg.resources) == 1
    res = cfg.resources[0]
    assert res.name == "card"
    assert res.uri_template == "axiom://demo/{id}"
    assert res.entry == "demo.module:read"


def test_parse_prompt_block(tmp_path: Path):
    """``[[extension.mcp.prompt]]`` parses with arguments list."""
    path = _write_manifest(
        tmp_path,
        '''
        [extension]
        name = "demo"
        version = "0.0.1"

        [extension.mcp]
        enabled = true

        [[extension.mcp.prompt]]
        name = "greet"
        description = "Greets the user."
        arguments = ["name", "tone"]
        entry = "demo.module:greet_prompt"
        ''',
    )
    cfg = parse_mcp_block(path)
    assert cfg is not None
    assert len(cfg.prompts) == 1
    p = cfg.prompts[0]
    assert p.name == "greet"
    assert tuple(p.arguments) == ("name", "tone")
    assert p.entry == "demo.module:greet_prompt"


# ---------------------------------------------------------------------------
# lint_mcp_block — opt-in OR opt-out is mandatory
# ---------------------------------------------------------------------------


def test_lint_missing_block_and_no_comment_fails(tmp_path: Path):
    """A manifest with neither ``[extension.mcp]`` nor opt-out comment fails lint."""
    path = _write_manifest(
        tmp_path,
        '''
        [extension]
        name = "demo"
        version = "0.0.1"
        ''',
    )
    errors = lint_mcp_block(path)
    assert any(isinstance(e, LintError) for e in errors)
    assert any("extension.mcp" in e.message for e in errors)


def test_lint_explicit_optout_passes(tmp_path: Path):
    """``enabled = false`` is a valid declaration and lint accepts it."""
    path = _write_manifest(
        tmp_path,
        '''
        [extension]
        name = "demo"
        version = "0.0.1"

        [extension.mcp]
        enabled = false
        ''',
    )
    errors = [e for e in lint_mcp_block(path) if isinstance(e, LintError)]
    assert errors == []


def test_lint_optout_via_comment_passes(tmp_path: Path):
    """The ``# mcp: not-applicable -- <reason>`` comment passes lint."""
    path = _write_manifest(
        tmp_path,
        '''
        # mcp: not-applicable -- pure CLI utility; nothing to expose
        [extension]
        name = "demo"
        version = "0.0.1"
        ''',
    )
    errors = [e for e in lint_mcp_block(path) if isinstance(e, LintError)]
    assert errors == []


def test_lint_collision_with_platform_warns(tmp_path: Path):
    """``mcp_name`` matching a platform-primitive name produces a lint error."""
    path = _write_manifest(
        tmp_path,
        '''
        [extension]
        name = "demo"
        version = "0.0.1"

        [extension.mcp]
        enabled = true
        prefix = "demo"

        [[extension.provides]]
        kind = "tool"
        name = "ping"
        description = "ping"

        [[extension.mcp.tool]]
        name = "ping"
        mcp_name = "axiom_memory__compose"
        ''',
    )
    errors = lint_mcp_block(path)
    assert any(isinstance(e, LintError) for e in errors)
    assert any("axiom_memory__compose" in e.message for e in errors)
