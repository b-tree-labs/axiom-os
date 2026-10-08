# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A tool a creator provides is exposed over MCP, or lint says why not (#1161).

A ``provides`` tool with ``[extension.mcp] enabled = true`` but no matching
``[[extension.mcp.tool]]`` passed lint and publish and was simply absent from
the assistant's tool list. And an MCP handler is called with ONE dict, so a
handler with keyword parameters failed only at call time.
"""

from __future__ import annotations

from pathlib import Path

from axiom.cli.ext.commands.lint import lint_extension

MANIFEST = """\
[extension]
name = "probe_ext"
version = "0.1.0"
kind = "tool"

[[extension.provides]]
kind = "tool"
name = "probe"
entry = "probe_ext.tools:probe"
description = "answer a probe"

[extension.mcp]
enabled = true
{mcp_tool}
"""


def _ext(tmp_path: Path, *, listed: bool, handler: str) -> Path:
    root = tmp_path / "probe_ext"
    (root / "probe_ext").mkdir(parents=True)
    (root / "probe_ext" / "__init__.py").write_text("")
    (root / "probe_ext" / "tools.py").write_text(handler)
    block = '\n[[extension.mcp.tool]]\nname = "probe"\n' if listed else ""
    (root / "axiom-extension.toml").write_text(MANIFEST.format(mcp_tool=block))
    return root


def _codes(root: Path) -> dict[str, str]:
    return {f.code: f.message for f in lint_extension(root)}


def test_an_unlisted_tool_is_named_as_not_exposed(tmp_path):
    codes = _codes(_ext(tmp_path, listed=False, handler="def probe(args):\n    return args\n"))
    assert "AEOS074" in codes and "probe" in codes["AEOS074"]


def test_a_listed_tool_with_a_one_dict_handler_is_clean(tmp_path):
    codes = _codes(_ext(tmp_path, listed=True, handler="def probe(args):\n    return args\n"))
    assert "AEOS074" not in codes and "AEOS075" not in codes


def test_a_handler_with_keyword_parameters_is_flagged(tmp_path):
    codes = _codes(
        _ext(tmp_path, listed=True, handler="def probe(site, channel='x'):\n    return site\n")
    )
    assert "AEOS075" in codes and "one dict" in codes["AEOS075"]


def test_a_tool_served_through_its_registered_skill_is_not_flagged(tmp_path):
    root = _ext(tmp_path, listed=False, handler="def probe(args):\n    return args\n")
    manifest = root / "axiom-extension.toml"
    manifest.write_text(
        manifest.read_text()
        + '\n[[extension.provides]]\nkind = "skill"\nname = "probe"\n'
        + 'entry = "probe_ext.tools:probe"\ndescription = "the same verb as a skill"\n'
    )
    assert "AEOS074" not in _codes(root)
