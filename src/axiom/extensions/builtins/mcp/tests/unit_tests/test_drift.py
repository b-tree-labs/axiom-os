# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Drift-detection tests for ``mcp.drift`` (spec §10).

Heartbeat-driven check that compares a cached MCP surface hash against
a freshly-computed hash; on divergence persisting past a debounce
window, M-O proposes an ``axi mcp regenerate`` via the spec-§10.3
RACI flow.

Tests pin the spec-mandated invariants:

- Drift detected after a manifest change (new tool added).
- No drift when cache and live hash match.
- Debounce: a single divergent heartbeat does not propose; the second
  consecutive divergence does.
- 3 nos = stop asking: the proposer disables itself for 24h after
  three back-to-back denials per ``feedback_raci_automation_escalation``.
- Acceptance regenerates the surface and clears the divergence.
"""

from __future__ import annotations

import json
from pathlib import Path


from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry
from axiom.extensions.builtins.mcp.drift import (
    DriftFinding,
    DriftProposer,
    accept_proposal,
    check_mcp_surface_drift,
)


def _write_cache(home: Path, surface) -> Path:
    cache = home / "mcp" / "surface.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(surface.to_dict(), indent=2, default=str),
        encoding="utf-8",
    )
    return cache


def _make_fixture_extension(make_extension, mcp_block_extra: str = ""):
    """Build a fixture extension with one tool surfaced via MCP."""
    body = """
        [extension]
        name = "drift_fixture"
        version = "0.0.1"
        description = "drift fixture"
        license = "Apache-2.0"
        owner = "axiom-tests"
        aeos_version = "0.1.0"

        [extension.mcp]
        enabled = true
        prefix = "drift_fix"

        [[extension.provides]]
        kind = "tool"
        name = "ping"
        entry = "axiom.extensions.builtins.mcp.platform_primitives:_node_hooks_list"
        description = "Returns pong."

        [[extension.mcp.tool]]
        name = "ping"
    """
    if mcp_block_extra:
        body += mcp_block_extra
    return make_extension("drift_fixture", body)


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


def test_drift_detected_after_manifest_change(tmp_axiom_home, make_extension):
    """A manifest change between cache write and re-walk produces a finding."""
    ext = _make_fixture_extension(make_extension)
    surface_v1 = AggregationRegistry(extensions=[ext]).build()
    _write_cache(tmp_axiom_home, surface_v1)

    # Mutate the manifest: add a second tool. The fresh hash will differ.
    extra = """
        [[extension.provides]]
        kind = "tool"
        name = "pong"
        entry = "axiom.extensions.builtins.mcp.platform_primitives:_node_hooks_list"
        description = "Returns ping."

        [[extension.mcp.tool]]
        name = "pong"
    """
    ext.manifest_path.write_text(
        ext.manifest_path.read_text() + extra, encoding="utf-8"
    )

    finding = check_mcp_surface_drift(
        node_root=tmp_axiom_home, extensions=[ext]
    )
    assert isinstance(finding, DriftFinding)
    assert finding.cached_hash == surface_v1.content_hash
    assert finding.fresh_hash != surface_v1.content_hash
    assert finding.kind == "mcp.surface.stale"


def test_drift_clean_when_synced(tmp_axiom_home, make_extension):
    """Hash equality between cached + fresh produces no finding."""
    ext = _make_fixture_extension(make_extension)
    surface = AggregationRegistry(extensions=[ext]).build()
    _write_cache(tmp_axiom_home, surface)

    finding = check_mcp_surface_drift(
        node_root=tmp_axiom_home, extensions=[ext]
    )
    assert finding is None


def test_drift_finding_when_no_cache_yet(tmp_axiom_home, make_extension):
    """A node with extensions but no cache file is treated as stale."""
    ext = _make_fixture_extension(make_extension)
    finding = check_mcp_surface_drift(
        node_root=tmp_axiom_home, extensions=[ext]
    )
    assert isinstance(finding, DriftFinding)
    assert finding.cached_hash is None
    assert finding.fresh_hash, finding


# ---------------------------------------------------------------------------
# Debounce: only propose after the second consecutive divergent heartbeat
# ---------------------------------------------------------------------------


def test_debounce_first_divergence_does_not_propose(
    tmp_axiom_home, make_extension
):
    """The first divergent heartbeat is recorded but no proposal fires."""
    ext = _make_fixture_extension(make_extension)
    surface = AggregationRegistry(extensions=[ext]).build()
    _write_cache(tmp_axiom_home, surface)
    ext.manifest_path.write_text(
        ext.manifest_path.read_text()
        + '\n[[extension.mcp.tool]]\nname = "ping"\n',
        encoding="utf-8",
    )

    proposer = DriftProposer(node_root=tmp_axiom_home)
    proposal = proposer.maybe_propose(extensions=[ext])
    assert proposal is None, proposal


def test_debounce_second_divergence_proposes(tmp_axiom_home, make_extension):
    """Two consecutive divergent heartbeats produce a proposal."""
    ext = _make_fixture_extension(make_extension)
    surface = AggregationRegistry(extensions=[ext]).build()
    _write_cache(tmp_axiom_home, surface)
    extra = """
        [[extension.provides]]
        kind = "tool"
        name = "pong"
        entry = "axiom.extensions.builtins.mcp.platform_primitives:_node_hooks_list"
        description = "Returns ping."

        [[extension.mcp.tool]]
        name = "pong"
    """
    ext.manifest_path.write_text(
        ext.manifest_path.read_text() + extra, encoding="utf-8"
    )

    proposer = DriftProposer(node_root=tmp_axiom_home)
    assert proposer.maybe_propose(extensions=[ext]) is None  # heartbeat 1
    proposal = proposer.maybe_propose(extensions=[ext])  # heartbeat 2
    assert proposal is not None
    assert proposal.action == "axi mcp regenerate"


# ---------------------------------------------------------------------------
# Three nos = stop asking (feedback_raci_automation_escalation)
# ---------------------------------------------------------------------------


def test_three_denials_disable_proposer(tmp_axiom_home, make_extension):
    """After three back-to-back denials the proposer stays silent."""
    ext = _make_fixture_extension(make_extension)
    surface = AggregationRegistry(extensions=[ext]).build()
    _write_cache(tmp_axiom_home, surface)
    ext.manifest_path.write_text(
        ext.manifest_path.read_text()
        + '\n[[extension.mcp.tool]]\nname = "ping"\n',
        encoding="utf-8",
    )

    proposer = DriftProposer(node_root=tmp_axiom_home)
    # Need to surface the proposal first (debounce burns one heartbeat).
    assert proposer.maybe_propose(extensions=[ext]) is None
    for _ in range(3):
        proposal = proposer.maybe_propose(extensions=[ext])
        assert proposal is not None
        proposer.record_denial(proposal)
    # Fourth heartbeat: still divergent, but the proposer is now silenced.
    assert proposer.maybe_propose(extensions=[ext]) is None


# ---------------------------------------------------------------------------
# Acceptance regenerates the surface
# ---------------------------------------------------------------------------


def test_node_health_audit_surfaces_drift_finding(tmp_axiom_home):
    """``hygiene.node_health.audit_node`` wires the MCP drift check.

    The wrapper walks live discovery (no injectable extensions arg),
    so on tmp_axiom_home with no cache it returns a stale-finding;
    after we write the cache to match the live surface it returns None.
    """
    from axiom.extensions.builtins.hygiene.node_health import (
        Severity,
        check_mcp_surface_drift_finding,
    )

    # No cache + extensions present on disk → stale.
    finding = check_mcp_surface_drift_finding()
    assert finding is not None
    assert finding.check == "mcp.surface.stale"
    assert finding.severity is Severity.INFO
    assert finding.auto_fixable is True

    # Sync the cache to whatever live discovery built.
    surface = AggregationRegistry.from_node().build()
    _write_cache(tmp_axiom_home, surface)
    assert check_mcp_surface_drift_finding() is None


def test_accept_proposal_regenerates_surface(tmp_axiom_home, make_extension):
    """Accepting the proposal rewrites the cache and clears the divergence."""
    ext = _make_fixture_extension(make_extension)
    surface_v1 = AggregationRegistry(extensions=[ext]).build()
    _write_cache(tmp_axiom_home, surface_v1)
    extra = """
        [[extension.provides]]
        kind = "tool"
        name = "pong"
        entry = "axiom.extensions.builtins.mcp.platform_primitives:_node_hooks_list"
        description = "Returns ping."

        [[extension.mcp.tool]]
        name = "pong"
    """
    ext.manifest_path.write_text(
        ext.manifest_path.read_text() + extra, encoding="utf-8"
    )

    accept_result = accept_proposal(
        node_root=tmp_axiom_home, extensions=[ext]
    )
    assert accept_result.new_hash != surface_v1.content_hash
    # Cache is now in sync — drift check returns None.
    assert (
        check_mcp_surface_drift(node_root=tmp_axiom_home, extensions=[ext])
        is None
    )
