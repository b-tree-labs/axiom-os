# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""End-to-end tests for the `neut pub` CLI.

These tests exercise the full stack: CLI argument parsing → engine → providers
→ filesystem side effects. They verify the user-facing behavior, not internals.

Two approaches used:
  1. subprocess.run — true external invocation (validates entry point, sys.path, imports)
  2. Direct main() calls — faster, still full-stack, easier to isolate with tmp dirs
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AXI_CLI = str(REPO_ROOT / "src" / "axiom" / "axiom_cli.py")


# ─── Subprocess Tests (true external invocation) ───


class TestCLISubprocess:
    """Tests that invoke the real CLI binary via subprocess."""

    def test_axi_help(self):
        """axi --help --all exits 0 and shows the full surface.

        Uses `--all` because tier-aware filtering (per
        `prd-axi-cli.md §Progressive Disclosure`) hides `status` from
        starter-tier users by default, and CI runs as a fresh first-run
        user. This test verifies the CLI is healthy + `status` is
        registered, not that it surfaces at the starter tier.
        """
        result = subprocess.run(
            [sys.executable, AXI_CLI, "--help", "--all"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        assert "axi" in result.stdout.lower()
        assert "status" in result.stdout
        assert "dr" in result.stdout

    def test_neut_unknown_subcommand(self):
        """neut <unknown> exits non-zero."""
        result = subprocess.run(
            [sys.executable, AXI_CLI, "bogus"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0
        assert "unknown" in result.stderr.lower() or "unknown" in result.stdout.lower()

    def test_doc_providers_subprocess(self):
        """neut pub providers lists registered providers."""
        result = subprocess.run(
            [sys.executable, AXI_CLI, "publish", "providers"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        assert "Generation" in result.stdout
        assert "Storage" in result.stdout
        assert "pandoc-docx" in result.stdout
        assert "local" in result.stdout

    def test_doc_status_subprocess(self):
        """neut pub status runs without error."""
        result = subprocess.run(
            [sys.executable, AXI_CLI, "publish", "status"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        # Should print either document table or "No tracked documents."
        assert "document" in result.stdout.lower() or "Doc ID" in result.stdout

    def test_doc_generate_subprocess(self, tmp_path):
        """neut pub generate produces a .docx file."""
        source = tmp_path / "e2e-gen-test.md"
        source.write_text("# E2E Generate\n\nThis tests generation.\n")

        result = subprocess.run(
            [sys.executable, AXI_CLI, "publish", "generate", str(source)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        assert "Generated" in result.stdout

        # Verify the .docx was created somewhere under generated/
        generated_dir = REPO_ROOT / "docs" / "_tools" / "generated"
        docx = generated_dir / "e2e-gen-test.docx"
        assert docx.exists()
        assert docx.stat().st_size > 0

        # Cleanup
        docx.unlink(missing_ok=True)


# ─── Full-stack Python tests (CLI main() with controlled args) ───


class TestDocflowFullPipeline:
    """End-to-end pipeline: generate → publish → status → check-links.

    Uses a temp directory as repo_root for full isolation.
    """

    @pytest.fixture
    def workspace(self, tmp_path):
        """Set up an isolated workspace with config and source docs."""
        # Create a .publisher.yaml
        config_yaml = tmp_path / ".publisher.yaml"
        config_yaml.write_text(
            "git:\n"
            "  require_clean: false\n"
            "  publish_branches: ['*']\n"
            "\n"
            "generation:\n"
            "  provider: pandoc-docx\n"
            "\n"
            "storage:\n"
            "  provider: local\n"
            "  local:\n"
            f"    base_dir: {tmp_path / 'published'}\n"
            "\n"
            "notifications:\n"
            "  provider: terminal\n"
        )

        # Create source documents
        docs_dir = tmp_path / "docs" / "specs"
        docs_dir.mkdir(parents=True)

        (docs_dir / "spec-a.md").write_text(
            "# Specification A\n\n"
            "## Overview\n\nThis is spec A.\n\n"
            "## Details\n\nSome detailed content.\n"
        )
        (docs_dir / "spec-b.md").write_text(
            "# Specification B\n\n"
            "## Overview\n\nThis is spec B.\n\n"
            "See also: [Spec A](spec-a.md)\n"
        )

        return tmp_path

    def _make_engine(self, workspace):
        """Create an engine rooted at the workspace."""
        from axiom.extensions.builtins.publishing.config import load_config

        config = load_config(workspace / ".publisher.yaml")
        config.repo_root = workspace
        config.git.publish_branches = ["*"]
        config.git.require_clean = False

        from axiom.extensions.builtins.publishing.engine import PublisherEngine
        return PublisherEngine(config)

    def test_generate_creates_docx(self, workspace):
        """Generate a .docx from markdown — file exists and has content."""
        engine = self._make_engine(workspace)
        source = workspace / "docs" / "specs" / "spec-a.md"

        output = engine.generate(source)

        assert output.exists()
        assert output.suffix == ".docx"
        assert output.stat().st_size > 0
        # Should preserve directory structure
        assert "specs" in str(output)

    def test_publish_creates_artifacts_and_state(self, workspace):
        """Full publish: generates docx, copies to storage, updates state + registry."""
        engine = self._make_engine(workspace)
        source = workspace / "docs" / "specs" / "spec-a.md"

        record = engine.publish(source)

        # 1. Publication record returned
        assert record is not None
        assert record.version == "v1"
        assert record.storage_provider == "local"
        assert record.url.startswith("file://")

        # 2. State file updated (always under .neut/publisher/)
        state_file = workspace / ".neut" / "publisher" / "publisher-state.json"
        assert state_file.exists()
        state_data = json.loads(state_file.read_text())
        doc_ids = [d["doc_id"] for d in state_data.get("documents", [])]
        assert "spec-a" in doc_ids

        # 3. Registry file updated
        registry_file = workspace / ".neut" / "publisher" / "publisher-registry.json"
        assert registry_file.exists()
        registry_data = json.loads(registry_file.read_text())
        registry_ids = [e.get("doc_id", "") for e in registry_data.get("documents", [])]
        assert "spec-a" in registry_ids

        # 4. Artifact exists in storage — publish mirrors docs/ subdir structure
        published_dir = workspace / "published"
        assert (published_dir / "spec-a.docx").exists()

    def test_publish_then_status(self, workspace):
        """Publish a doc, then status shows it as published."""
        engine = self._make_engine(workspace)
        source = workspace / "docs" / "specs" / "spec-a.md"

        engine.publish(source)
        docs = engine.status()

        assert len(docs) == 1
        assert docs[0].doc_id == "spec-a"
        assert docs[0].status == "published"
        assert docs[0].published.version == "v1"

    def test_publish_then_check_links(self, workspace):
        """After publishing, check-links reports the source file as valid."""
        engine = self._make_engine(workspace)
        source = workspace / "docs" / "specs" / "spec-a.md"

        engine.publish(source)
        results = engine.check_links()

        assert "docs/specs/spec-a.md" in results["valid"]
        assert results["missing"] == []

    def test_multi_doc_publish_pipeline(self, workspace):
        """Publish two docs, verify both appear in status and registry."""
        engine = self._make_engine(workspace)

        engine.publish(workspace / "docs" / "specs" / "spec-a.md")
        engine.publish(workspace / "docs" / "specs" / "spec-b.md")

        docs = engine.status()
        assert len(docs) == 2
        ids = {d.doc_id for d in docs}
        assert ids == {"spec-a", "spec-b"}

        link_map = engine.registry.build_link_map()
        assert any("spec-a" in k for k in link_map)
        assert any("spec-b" in k for k in link_map)

    def test_version_increment_across_publishes(self, workspace):
        """Re-publishing the same doc increments the version."""
        engine = self._make_engine(workspace)
        source = workspace / "docs" / "specs" / "spec-a.md"

        r1 = engine.publish(source)
        assert r1.version == "v1"

        # Modify and republish
        source.write_text("# Specification A (revised)\n\nUpdated content.\n")
        r2 = engine.publish(source)
        assert r2.version == "v2"

        # Status reflects latest version
        docs = engine.status()
        assert docs[0].published.version == "v2"

    def test_draft_publish_pipeline(self, workspace):
        """Draft publish sets status to draft with active_draft record."""
        engine = self._make_engine(workspace)
        source = workspace / "docs" / "specs" / "spec-a.md"

        record = engine.publish(source, draft=True)

        assert record is not None
        docs = engine.status()
        doc = docs[0]
        assert doc.status == "draft"
        assert doc.active_draft is not None
        assert doc.active_draft.version == "v1"

    def test_draft_then_promote_to_published(self, workspace):
        """First publish as draft, then publish for real — version increments."""
        engine = self._make_engine(workspace)
        source = workspace / "docs" / "specs" / "spec-a.md"

        r_draft = engine.publish(source, draft=True)
        assert r_draft.version == "v1"

        r_pub = engine.publish(source)
        assert r_pub.version == "v2"

        docs = engine.status()
        assert docs[0].status == "published"
        assert docs[0].published.version == "v2"

    def test_state_persists_across_engine_instances(self, workspace):
        """State written by one engine instance is readable by another."""
        engine1 = self._make_engine(workspace)
        source = workspace / "docs" / "specs" / "spec-a.md"
        engine1.publish(source)

        # Create a fresh engine (simulates re-running the CLI)
        engine2 = self._make_engine(workspace)
        docs = engine2.status()
        assert len(docs) == 1
        assert docs[0].doc_id == "spec-a"
        assert docs[0].status == "published"

    def test_check_links_detects_missing_source(self, workspace):
        """If a registered source file is deleted, check-links reports it missing."""
        engine = self._make_engine(workspace)
        source = workspace / "docs" / "specs" / "spec-a.md"
        engine.publish(source)

        # Delete the source file
        source.unlink()

        results = engine.check_links()
        assert "docs/specs/spec-a.md" in results["missing"]
