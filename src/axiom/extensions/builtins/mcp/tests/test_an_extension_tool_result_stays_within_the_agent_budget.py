# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A manifest-declared MCP tool's result is bounded like a registry tool's (ADR-157).

Registry-backed tools pass through ``serving_guard.bound_bytes`` with the agent budget; a manifest tool's result
went to the client whole, so an extension returning a long list could fill a client's context window. A result
that fits is returned unchanged.
"""

from __future__ import annotations

import asyncio
import json

from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry
from axiom.extensions.builtins.mcp.server import _raw_dispatch
from axiom.extensions.contracts import parse_manifest
from axiom.infra import serving_guard as sg
from axiom.infra.skills import SkillRegistry

MODULE = __name__


def big(args):
    return {"ok": True, "results": [{"i": i, "text": "x" * 500} for i in range(1000)]}


def small(args):
    return {"ok": True, "results": [1, 2, 3]}


def big_list(args):
    return [{"i": i, "text": "y" * 500} for i in range(1000)]


MANIFEST = """
[extension]
name = "probe_size"
version = "0.0.1"
""" + "".join(f"""
[[extension.provides]]
kind = "tool"
name = "{n}"
entry = "{MODULE}:{n}"
idempotent = true
""" for n in ("big", "small", "big_list")) + """
[extension.mcp]
enabled = true
""" + "".join(f"""
[[extension.mcp.tool]]
name = "{n}"
""" for n in ("big", "small", "big_list"))


def _call(tmp_path, monkeypatch, tool):
    monkeypatch.delenv("AXIOM_MCP_CLIENT_PRINCIPAL", raising=False)  # the local owner
    (tmp_path / "axiom-extension.toml").write_text(MANIFEST, encoding="utf-8")
    surface = AggregationRegistry([parse_manifest(tmp_path / "axiom-extension.toml")], registry=SkillRegistry()).build()
    return asyncio.run(_raw_dispatch(surface, f"axiom_probe_size__{tool}", {}))


def _budget():
    return sg.load_policy().budget(cost_class="lookup", caller_class="agent").max_bytes


def test_a_large_result_is_thinned_and_says_so(tmp_path, monkeypatch):
    out = _call(tmp_path, monkeypatch, "big")
    assert len(json.dumps(out)) <= _budget()
    assert 0 < len(out["results"]) < 1000
    assert any("reduced" in n for n in out["notes"])


def test_a_result_that_fits_is_unchanged(tmp_path, monkeypatch):
    assert _call(tmp_path, monkeypatch, "small") == {"ok": True, "results": [1, 2, 3]}


def test_a_large_non_dict_result_is_bounded_too(tmp_path, monkeypatch):
    out = _call(tmp_path, monkeypatch, "big_list")
    assert len(json.dumps(out)) <= _budget()
    assert 0 < len(out["value"]) < 1000
