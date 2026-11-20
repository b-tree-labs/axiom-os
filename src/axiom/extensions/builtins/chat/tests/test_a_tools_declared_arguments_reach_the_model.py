# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A tool's declared arguments reach the model chat gives it to.

AEOS declares a tool's argument schema as ``input_schema`` (a ``pkg.mod:attr``
reference) and its MCP entry's as ``input_schema_module``. Chat read only a
``parameters`` key the standard does not define, so every manifest tool reached
the model with no argument names. A model asked for a week's statistics passed
``window=…``, the tool read only ``start``/``end``, and the answer covered the
default last day.
"""

from __future__ import annotations

import sys
import textwrap

import pytest

from axiom.extensions.builtins.chat import tools as T

_SCHEMA = {
    "type": "object",
    "properties": {"start": {"type": "string"}, "end": {"type": "string"}},
    "required": ["start"],
}


@pytest.fixture
def ext(tmp_path, monkeypatch):
    (tmp_path / "declared_schemas.py").write_text(f"WINDOW = {_SCHEMA!r}\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield tmp_path
    sys.modules.pop("declared_schemas", None)


def _manifest(root, body: str):
    (root / "axiom-extension.toml").write_text(
        '[extension]\nname = "demo"\nversion = "0.1.0"\n\n' + textwrap.dedent(body)
    )
    return root


_TOOL = """
[[extension.provides]]
kind = "tool"
name = "stat"
entry = "json:dumps"
description = "A statistic over a window."
idempotent = true
"""


def test_the_tools_input_schema_reference_is_what_the_model_sees(ext):
    _manifest(ext, _TOOL + 'input_schema = "declared_schemas:WINDOW"\n')
    assert T._scan_manifest_tools(roots=[ext])["stat"].parameters == _SCHEMA


def test_the_mcp_entrys_schema_module_is_used_when_the_tool_names_none(ext):
    _manifest(
        ext,
        _TOOL + '\n[[extension.mcp.tool]]\nname = "stat"\n'
        'input_schema_module = "declared_schemas:WINDOW"\n',
    )
    assert T._scan_manifest_tools(roots=[ext])["stat"].parameters == _SCHEMA


def test_a_manifest_that_used_parameters_still_works(ext):
    _manifest(
        ext,
        _TOOL + '\n[extension.provides.parameters]\ntype = "object"\n'
        '[extension.provides.parameters.properties.start]\ntype = "string"\n',
    )
    params = T._scan_manifest_tools(roots=[ext])["stat"].parameters
    assert params["properties"] == {"start": {"type": "string"}}


def test_a_reference_that_does_not_resolve_does_not_drop_the_tool(ext):
    _manifest(ext, _TOOL + 'input_schema = "declared_schemas:NOT_THERE"\n')
    found = T._scan_manifest_tools(roots=[ext])
    assert "stat" in found
    assert found["stat"].parameters == {}
