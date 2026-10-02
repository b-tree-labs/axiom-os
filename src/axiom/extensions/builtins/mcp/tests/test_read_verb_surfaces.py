# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""P8 — read verbs are projected onto MCP; writes are NOT (bounded exposure, ADR-072 §4.9.4).

An MCP client should reach the diagnostic/list/status reads of the ops extensions, but a
write (ingest, clean, cut, …) must not be exposed by default.
"""
from __future__ import annotations

import importlib

import pytest

from axiom.extensions.builtins.mcp.skill_tools import is_mcp_exposed
from axiom.infra.skills import SkillRegistry

CASES = {
    "data_platform": {
        "reads": ["data.diagnose", "data.list", "data.troubleshoot", "data.backup_validate"],
        "writes": ["data.ingest", "data.register", "data.backup", "data.unregister"],
    },
    "hygiene": {
        "reads": ["hygiene.status", "hygiene.diagnose", "hygiene.discover", "hygiene.ls"],
        "writes": ["hygiene.clean", "hygiene.purge", "hygiene.deny"],
    },
    "release": {
        "reads": ["release.status", "release.check", "release.plan", "release.list"],
        "writes": ["release.cut", "release.pause", "release.close"],
    },
    "authz": {
        # audit-query reads are exposed; decide (enforcement/receipt) + graduation are not.
        "reads": ["audit.list", "audit.show", "audit.chain", "audit.causes", "audit.explain",
                  "audit.actions", "audit.healthcheck"],
        "writes": ["audit.graduation", "audit.lint"],
    },
}


def _bound_registry(ext: str) -> SkillRegistry:
    mod = importlib.import_module(f"axiom.extensions.builtins.{ext}.skills")
    r = SkillRegistry()
    mod.bind(r)
    return r


@pytest.mark.parametrize("ext", sorted(CASES))
def test_reads_exposed_on_mcp(ext):
    r = _bound_registry(ext)
    specs = r.specs()
    for name in CASES[ext]["reads"]:
        assert name in specs, f"{name} not registered"
        assert is_mcp_exposed(specs[name]), f"{name} should be MCP-exposed"


@pytest.mark.parametrize("ext", sorted(CASES))
def test_writes_not_on_mcp(ext):
    r = _bound_registry(ext)
    specs = r.specs()
    for name in CASES[ext]["writes"]:
        # a write is either registered bare (no spec) or a spec without the mcp surface;
        # either way it must not project onto MCP.
        assert name not in specs or not is_mcp_exposed(specs[name]), f"{name} must NOT be on MCP"


def test_press_egress_writes_returned_to_mcp_behind_the_effect_gate():
    """ADR-114 §3: the external-egress writes are BACK on MCP, now gated by the
    built-in propose-on-mcp approval floor (authority.egress_write_mcp_rules),
    replacing the interim P8/B withholding. Their being on the surface is the
    doctrine-correct state; the effect gate — not surface removal — is what
    holds them for approval."""
    from axiom.infra.authority import EGRESS_WRITE_MCP_APPROVAL_TOOLS
    from axiom.extensions.builtins.publishing.skills import bind

    r = SkillRegistry()
    bind(r)
    specs = r.specs()
    for name in ("press.publish", "press.mirror_sync"):
        assert name in specs and is_mcp_exposed(specs[name]), f"{name} should be back on MCP"
        # and covered by the built-in egress approval floor
        assert name in EGRESS_WRITE_MCP_APPROVAL_TOOLS, f"{name} must be in the approval floor"
    for name in ("press.draft", "press.mirror_status", "press.standards"):
        assert is_mcp_exposed(specs[name]), f"{name} should remain on MCP"


def test_spec_section_8_matches_the_platform_surface():
    """The spec's platform-tool table must name exactly the tools that exist.

    It had drifted badly — three tools documented that no longer exist, four
    real ones absent — because a hand-maintained table has nothing keeping it
    honest. An operator reading it would have called a tool that isn't there.
    """
    import pathlib
    import re

    from axiom.extensions.builtins.mcp.platform_primitives import PLATFORM_TOOL_NAMES

    spec = pathlib.Path(__file__).resolve().parents[4].parents[1] / "docs" / "specs" / "spec-builtin-mcp-server.md"
    if not spec.exists():  # packaged install without docs
        import pytest

        pytest.skip("spec not present in this layout")
    text = spec.read_text(encoding="utf-8")
    section = text[text.index("## 8. Platform primitives") : text.index("## 9.")]
    documented = set(re.findall(r"\|\s*`(axiom_[a-z_]+)`\s*\|", section))
    assert documented == set(PLATFORM_TOOL_NAMES), (
        f"spec §8 drifted — documented-but-absent: {sorted(documented - set(PLATFORM_TOOL_NAMES))}; "
        f"real-but-undocumented: {sorted(set(PLATFORM_TOOL_NAMES) - documented)}"
    )
