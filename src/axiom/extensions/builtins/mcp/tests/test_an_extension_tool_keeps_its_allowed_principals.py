# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An extension's ``[[extension.mcp.tool]] allowed_principals`` reaches the identity gate (ADR-114 §1).

The manifest parser read the patterns, but the surface recorded them only for registry tools, so every manifest
tool fell back to the gate's owner-only default: a tool declared ``["@*:*"]`` still refused other principals, and a
tool declared for one named principal admitted only the owner.
"""

from __future__ import annotations

import asyncio

from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry
from axiom.extensions.builtins.mcp.server import _raw_dispatch
from axiom.extensions.contracts import parse_manifest
from axiom.infra.skills import SkillRegistry

MODULE = __name__


def echo(args):
    return {"ok": True, "echo": args}


MANIFEST = f"""
[extension]
name = "probe_ext"
version = "0.0.1"

[[extension.provides]]
kind = "tool"
name = "open_to_all"
entry = "{MODULE}:echo"
idempotent = true

[[extension.provides]]
kind = "tool"
name = "default_patterns"
entry = "{MODULE}:echo"
idempotent = true

[[extension.provides]]
kind = "tool"
name = "one_service"
entry = "{MODULE}:echo"
idempotent = true

[extension.mcp]
enabled = true

[[extension.mcp.tool]]
name = "open_to_all"
allowed_principals = ["@*:*"]

[[extension.mcp.tool]]
name = "default_patterns"

[[extension.mcp.tool]]
name = "one_service"
allowed_principals = ["@ingest:acme"]
"""


def _surface(tmp_path):
    (tmp_path / "axiom-extension.toml").write_text(MANIFEST, encoding="utf-8")
    ext = parse_manifest(tmp_path / "axiom-extension.toml")
    return AggregationRegistry([ext], registry=SkillRegistry()).build()


def test_the_declared_patterns_are_on_the_surface(tmp_path):
    allowed = _surface(tmp_path).allowed_principals
    assert allowed["axiom_probe_ext__open_to_all"] == ("@*:*",)
    assert allowed["axiom_probe_ext__default_patterns"] == ("@*:local",)
    assert allowed["axiom_probe_ext__one_service"] == ("@ingest:acme",)


def test_the_gate_applies_them(tmp_path, monkeypatch):
    surface = _surface(tmp_path)
    monkeypatch.setenv("AXIOM_MCP_CLIENT_PRINCIPAL", "@ingest:acme")
    for tool, admitted in (("open_to_all", True), ("default_patterns", False), ("one_service", True)):
        out = asyncio.run(_raw_dispatch(surface, f"axiom_probe_ext__{tool}", {"q": 1}))
        assert (out.get("refused") is not True) is admitted, (tool, out)
