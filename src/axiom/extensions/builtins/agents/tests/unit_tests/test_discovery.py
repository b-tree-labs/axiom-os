# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axi agents` discovery — every extension with an [agent]
section must be visible, regardless of its top-level `kind`.

Regression: prior filter required `ext.kind == "agent"`, but the
extension manifest's top-level `kind` defaults to "tool" for *every*
agent-bearing extension we ship (release/hygiene/publishing/signals/
diagnostics). The filter therefore returned an empty list and
`axi agents status` reported "No agent extensions ... found" even
though several were declared.
"""

from __future__ import annotations

from axiom.extensions.builtins.agents.cli import _discover_agent_extensions


def test_discovery_includes_release_rivet():
    """The release extension (RIVET) declares [agent] -> must be discovered."""
    names = {e.name for e in _discover_agent_extensions()}
    assert "release" in names, (
        f"RIVET (release ext) missing from agent discovery; got {sorted(names)}"
    )


def test_discovery_includes_all_agent_bearing_axi_platform_builtins():
    """Every axi-platform builtin with a real [agent] section is expected.

    Note: model_corral is a consumer-extension, not axi-platform; only
    discoverable when both packages are installed (e.g., the configured coordinator
    deployment). This test asserts axi-platform's own extensions only.
    """
    names = {e.name for e in _discover_agent_extensions()}
    expected = {"release", "hygiene", "publishing", "signals", "diagnostics"}
    missing = expected - names
    assert not missing, f"missing agent extensions from discovery: {sorted(missing)}"


def test_discovered_extensions_all_have_agent_section():
    """Filter must not include extensions without an [agent] section."""
    for ext in _discover_agent_extensions():
        assert ext.agent is not None, (
            f"discovery returned {ext.name} which has no [agent] section"
        )
