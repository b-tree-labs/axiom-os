# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for P2 item 6 — cross-provider migration (ADR-087 D2/D9).

The P0 export/import path driven end-to-end across *providers*, not
just accounts: absorb from harness-native stores on node A, export a
signed bundle, ``import --assume-principal`` into a second scope on
node B. Per-source extraction on the destination returns exactly the
fragments that entered from each source — origin coordinates survive
re-homing (D1), and the alias set of a merge rides the bundle so folded
coordinates keep resolving after migration (D3: no silent loss).
"""

from __future__ import annotations

from pathlib import Path

import pytest

SRC_PRINCIPAL = "@alice:personal"
DST_PRINCIPAL = "@alice:work"


def _make_composition(base: Path):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    base.mkdir(parents=True, exist_ok=True)
    kp = generate_keypair()
    reg = ArtifactRegistry(backend=SQLiteBackend(base / "artifacts.db"))
    audit = AuditLog(base / "audit.jsonl", signing_keypair=kp)
    return CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


@pytest.fixture
def claude_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    memdir = home / ".claude" / "projects" / "-Users-alice-proj" / "memory"
    memdir.mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text(
        "# Global instructions\n\nAlways answer briefly.\n"
    )
    (memdir / "feedback_uv.md").write_text(
        "---\nname: uv preference\n"
        "description: Prefers uv for python installs\n"
        "type: feedback\n---\n\nPrefers uv for python installs\n"
    )
    return home


@pytest.fixture
def goose_base(tmp_path: Path) -> Path:
    base = tmp_path / "goose-memory"
    base.mkdir(parents=True)
    # First entry is a planted exact duplicate of the Claude topic file
    # (same normalized text) so a cross-source collapse + alias exists
    # before migration. Second entry is unique to this source.
    (base / "development.txt").write_text(
        "Prefers uv for python installs\n"
        "\n"
        "Runs the linter before every commit.\n"
    )
    return base


@pytest.fixture
def migrated(claude_home, goose_base, tmp_path):
    """Absorb on node A (with dedup) → export → import on node B."""
    from axiom.extensions.builtins.memory.skills.export_bundle import (
        export_bundle,
    )
    from axiom.extensions.builtins.memory.skills.import_bundle import (
        import_bundle,
    )
    from axiom.memory.absorb.importer import import_candidates
    from axiom.memory.absorb.markdown_hierarchy import claude_code_adapter
    from axiom.memory.absorb.structured_store import goose_adapter
    from axiom.memory.dedup import DedupEngine

    node_a = _make_composition(tmp_path / "node-a")
    engine = DedupEngine(embedder=None)

    claude = claude_code_adapter(account="acct-claude", home=claude_home)
    report_a = import_candidates(
        node_a, claude.scan().candidates,
        principal=SRC_PRINCIPAL, dedup=engine,
    )
    goose = goose_adapter(account="acct-goose", base=goose_base)
    report_b = import_candidates(
        node_a, goose.scan().candidates,
        principal=SRC_PRINCIPAL, dedup=engine,
    )
    # Sanity: the planted duplicate collapsed cross-source on node A.
    assert report_a.imported == 2
    assert report_b.collapsed_exact == 1

    bundle = tmp_path / "bundle.tar.gz"
    sessions = tmp_path / "no-sessions"
    sessions.mkdir()
    result = export_bundle({
        "composition": node_a,
        "principal": SRC_PRINCIPAL,
        "out": str(bundle),
        "sessions_dir": str(sessions),
    }, None)
    assert result.ok, result.errors

    node_b = _make_composition(tmp_path / "node-b")
    imported = import_bundle({
        "composition": node_b,
        "bundle": str(bundle),
        "assume_principal": DST_PRINCIPAL,
        "sessions_dir": str(tmp_path / "b-sessions"),
    }, None)
    assert imported.ok, imported.errors
    return node_a, node_b, imported.value, bundle


class TestCrossProviderMigration:
    def test_fragments_rehomed_with_origins_preserved(self, migrated):
        node_a, node_b, value, _ = migrated
        # 3 live fragments on A (2 claude + 1 unique goose; dup folded).
        live_b = node_b.artifact_registry.list(kind="fragment")
        assert value["imported"] == 3
        assert len(live_b) == 3
        for artifact in live_b:
            data = artifact.data
            # Re-homed ownership, preserved origin coordinate.
            assert data["ownership"]["master"] == DST_PRINCIPAL
            origin = data["provenance"]["origin"]
            assert origin["harness"] in {"claude-code", "goose"}

    def test_per_source_extraction_on_destination(self, migrated):
        from axiom.memory.absorb.extract import extract_by_source

        node_a, node_b, _, _ = migrated
        got_claude = extract_by_source(
            node_b, harness="claude-code", account="acct-claude",
        )
        assert len(got_claude) == 2
        # The goose source entered two memories: one unique fragment
        # plus one folded into the claude canonical — the alias rides
        # the bundle, so extraction still returns exactly both.
        got_goose = extract_by_source(
            node_b, harness="goose", account="acct-goose",
        )
        assert len(got_goose) == 2
        texts = {f.content.get("text", "") for f in got_goose}
        assert any("linter" in t for t in texts)
        assert any("uv for python installs" in t.casefold() for t in texts)

    def test_alias_resolves_to_same_canonical_as_source_node(self, migrated):
        from axiom.memory.absorb.extract import extract_by_source

        node_a, node_b, _, _ = migrated
        canon_a = {
            f.id
            for f in extract_by_source(
                node_a, harness="goose", account="acct-goose",
            )
        }
        canon_b = {
            f.id
            for f in extract_by_source(
                node_b, harness="goose", account="acct-goose",
            )
        }
        # Fragment ids are preserved by import, so the alias points at
        # the same canonical id on both nodes.
        assert canon_a == canon_b

    def test_reimport_is_noop(self, migrated, tmp_path):
        from axiom.extensions.builtins.memory.skills.import_bundle import (
            import_bundle,
        )

        _, node_b, _, bundle = migrated
        again = import_bundle({
            "composition": node_b,
            "bundle": str(bundle),
            "assume_principal": DST_PRINCIPAL,
            "sessions_dir": str(tmp_path / "b-sessions-2"),
        }, None)
        assert again.ok
        assert again.value["imported"] == 0
        assert again.value["skipped_duplicate"] == 3
        assert len(node_b.artifact_registry.list(kind="fragment")) == 3
