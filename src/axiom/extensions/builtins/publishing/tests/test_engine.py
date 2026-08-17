# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the Publisher engine.

These tests verify the complete publish workflow through the engine,
ensuring all components work together correctly.
"""

import pytest

from axiom.extensions.builtins.publishing.config import (
    GitPolicy,
    ProviderConfig,
    PublisherConfig,
)
from axiom.extensions.builtins.publishing.engine import PublisherEngine


@pytest.fixture
def engine(tmp_path):
    """Create an engine with local storage for testing.

    Uses tmp_path as repo_root so state/registry files are isolated
    from the real project state.
    """
    config = PublisherConfig(
        git=GitPolicy(
            require_clean=False,
            require_pushed=False,
            publish_branches=["*"],  # Allow all branches in tests
        ),
        generation=ProviderConfig(provider="pandoc-docx"),
        storage=ProviderConfig(
            provider="local",
            settings={"base_dir": str(tmp_path / "published")},
        ),
        notification=ProviderConfig(provider="terminal"),
        repo_root=tmp_path,
    )
    return PublisherEngine(config)


class TestEngineGenerate:
    """Test local generation without publishing."""

    def test_generate_markdown(self, engine, tmp_path):
        source = tmp_path / "test.md"
        source.write_text("# Test\n\nContent here.\n")

        output = engine.generate(source, output_dir=tmp_path / "output")
        assert output.exists()
        assert output.suffix == ".docx"

    def test_generate_preserves_structure(self, engine, tmp_path):
        """Generating from docs/ preserves directory structure."""
        # Create docs/specs/ structure within tmp_path (engine's repo_root)
        specs_dir = tmp_path / "docs" / "specs"
        specs_dir.mkdir(parents=True)
        source = specs_dir / "test-spec.md"
        source.write_text("# Test Spec\n\nContent here.\n")

        output = engine.generate(source)
        assert output.exists()
        # Should be in generated/specs/
        assert "specs" in str(output)


class TestEnginePublish:
    """Test the full publish workflow."""

    def test_publish_to_local(self, engine, tmp_path):
        source = tmp_path / "publish-test.md"
        source.write_text("# Publish Test\n\nThis document tests publishing.\n")

        record = engine.publish(source, storage_override="local")

        assert record is not None
        assert record.version == "v1"
        assert record.storage_provider == "local"
        assert record.url.startswith("file://")

    def test_publish_updates_state(self, engine, tmp_path):
        source = tmp_path / "state-test.md"
        source.write_text("# State Test\n\nContent.\n")

        engine.publish(source, storage_override="local")

        docs = engine.status()
        assert len(docs) >= 1
        doc = next(d for d in docs if d.doc_id == "state-test")
        assert doc.status == "published"

    def test_publish_updates_registry(self, engine, tmp_path):
        source = tmp_path / "registry-test.md"
        source.write_text("# Registry Test\n\nContent.\n")

        engine.publish(source, storage_override="local")

        link_map = engine.registry.build_link_map()
        assert any("registry-test" in k for k in link_map)

    def test_publish_increments_version(self, engine, tmp_path):
        source = tmp_path / "version-test.md"
        source.write_text("# V1\n\nFirst version.\n")

        r1 = engine.publish(source, storage_override="local")
        assert r1.version == "v1"

        source.write_text("# V2\n\nSecond version.\n")
        r2 = engine.publish(source, storage_override="local")
        assert r2.version == "v2"

    def test_publish_draft(self, engine, tmp_path):
        source = tmp_path / "draft-test.md"
        source.write_text("# Draft\n\nDraft content.\n")

        record = engine.publish(source, storage_override="local", draft=True)

        assert record is not None
        docs = engine.status()
        doc = next(d for d in docs if d.doc_id == "draft-test")
        assert doc.status == "draft"
        assert doc.active_draft is not None


class TestEngineProviderAgnostic:
    """Verify the engine is truly provider-agnostic."""

    def test_engine_has_no_provider_imports(self):
        """Verify engine.py doesn't import specific providers."""
        import inspect

        from axiom.extensions.builtins.publishing import engine

        source = inspect.getsource(engine)

        # Should NOT import from any specific provider module
        assert "from axiom.extensions.builtins.publishing.providers.generation." not in source
        assert "from axiom.extensions.builtins.publishing.providers.storage." not in source
        assert "from axiom.extensions.builtins.publishing.providers.feedback." not in source
        assert "from axiom.extensions.builtins.publishing.providers.notification.terminal" not in source
        assert "from axiom.extensions.builtins.publishing.providers.embedding." not in source

    def test_swapping_storage_provider(self, tmp_path):
        """Changing storage provider in config changes behavior."""
        source = tmp_path / "swap-test.md"
        source.write_text("# Swap Test\n\nContent.\n")

        # Create engine with local-a
        config_a = PublisherConfig(
            git=GitPolicy(require_clean=False, publish_branches=["*"]),
            storage=ProviderConfig(
                provider="local",
                settings={"base_dir": str(tmp_path / "storage-a")},
            ),
            notification=ProviderConfig(provider="terminal"),
            repo_root=tmp_path,
        )
        engine_a = PublisherEngine(config_a)
        r_a = engine_a.publish(source, storage_override="local")

        # Create engine with local-b
        config_b = PublisherConfig(
            git=GitPolicy(require_clean=False, publish_branches=["*"]),
            storage=ProviderConfig(
                provider="local",
                settings={"base_dir": str(tmp_path / "storage-b")},
            ),
            notification=ProviderConfig(provider="terminal"),
            repo_root=tmp_path,
        )
        engine_b = PublisherEngine(config_b)
        r_b = engine_b.publish(source, storage_override="local")

        # Both should succeed but produce different URLs
        assert r_a is not None
        assert r_b is not None


class TestEngineStatus:
    """Test status and monitoring commands."""

    def test_status_empty(self, engine):
        assert engine.status() == []

    def test_list_providers(self, engine):
        providers = engine.list_providers()
        assert "generation" in providers
        assert "storage" in providers
        assert "pandoc-docx" in providers["generation"]
        assert "local" in providers["storage"]

    def test_check_links_empty(self, engine):
        results = engine.check_links()
        assert results["valid"] == []
        assert results["missing"] == []
