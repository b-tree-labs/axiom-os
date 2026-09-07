# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for pack lifecycle management — full semver + rollback.

TDD: tests before implementation.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path


class TestPackManifestVersioning:
    def test_manifest_has_format_version(self):
        """PackManifest must include format_version field."""
        from axiom.vega.federation.packs import PackManifest

        m = PackManifest(
            pack_id="test", version="1.0.0", content_type="rag",
            format_version="2.0.0",
        )
        assert m.format_version == "2.0.0"

    def test_manifest_has_compatible_axiom_versions(self):
        from axiom.vega.federation.packs import PackManifest

        m = PackManifest(
            pack_id="test", version="1.0.0", content_type="rag",
            compatible_axiom_versions=">=0.7.0",
        )
        assert m.compatible_axiom_versions == ">=0.7.0"

    def test_manifest_serialization_roundtrip(self):
        from axiom.vega.federation.packs import PackManifest

        m = PackManifest(
            pack_id="community-knowledge", version="2.0.0",
            content_type="rag+graph", format_version="2.0.0",
            compatible_axiom_versions=">=0.7.0",
        )
        d = m.to_dict()
        assert d["format_version"] == "2.0.0"
        assert d["compatible_axiom_versions"] == ">=0.7.0"

        m2 = PackManifest.from_dict(d)
        assert m2.format_version == "2.0.0"
        assert m2.compatible_axiom_versions == ">=0.7.0"

    def test_old_manifest_defaults_format_version_1(self):
        """Manifests without format_version default to '1.0.0'."""
        from axiom.vega.federation.packs import PackManifest

        old_data = {"pack_id": "test", "version": "1.0.0", "content_type": "rag"}
        m = PackManifest.from_dict(old_data)
        assert m.format_version == "1.0.0"


class TestPackCompatibility:
    def test_compatible_version_accepted(self):
        from axiom.vega.federation.pack_lifecycle import check_pack_compatibility

        assert check_pack_compatibility(
            pack_format="2.0.0",
            node_supported_formats=["1.0.0", "2.0.0"],
        ) is True

    def test_incompatible_version_rejected(self):
        from axiom.vega.federation.pack_lifecycle import check_pack_compatibility

        assert check_pack_compatibility(
            pack_format="3.0.0",
            node_supported_formats=["1.0.0", "2.0.0"],
        ) is False

    def test_format_1_always_compatible(self):
        """Format 1.0.0 packs are always installable (backward compat)."""
        from axiom.vega.federation.pack_lifecycle import check_pack_compatibility

        assert check_pack_compatibility(
            pack_format="1.0.0",
            node_supported_formats=["1.0.0", "2.0.0"],
        ) is True


class TestPackRollback:
    def test_previous_version_retained(self):
        """Installing a new version keeps the previous one."""
        from axiom.vega.federation.pack_lifecycle import PackLifecycleManager

        with tempfile.TemporaryDirectory() as tmp:
            mgr = PackLifecycleManager(install_dir=Path(tmp))
            # Simulate v1 installed
            v1_dir = Path(tmp) / "community-knowledge" / "1.0.0"
            v1_dir.mkdir(parents=True)
            (v1_dir / "manifest.json").write_text('{"version": "1.0.0"}')

            versions = mgr.list_versions("community-knowledge")
            assert "1.0.0" in versions

    def test_rollback_restores_previous(self):
        from axiom.vega.federation.pack_lifecycle import PackLifecycleManager

        with tempfile.TemporaryDirectory() as tmp:
            mgr = PackLifecycleManager(install_dir=Path(tmp))

            # Install v1 and v2
            for v in ["1.0.0", "2.0.0"]:
                d = Path(tmp) / "test-pack" / v
                d.mkdir(parents=True)
                (d / "manifest.json").write_text(json.dumps({"version": v}))

            # Active should be latest
            assert mgr.get_active_version("test-pack") == "2.0.0"

            # Rollback to v1
            mgr.rollback("test-pack", "1.0.0")
            assert mgr.get_active_version("test-pack") == "1.0.0"

    def test_list_versions_sorted(self):
        from axiom.vega.federation.pack_lifecycle import PackLifecycleManager

        with tempfile.TemporaryDirectory() as tmp:
            mgr = PackLifecycleManager(install_dir=Path(tmp))
            for v in ["1.0.0", "2.0.0", "1.1.0"]:
                d = Path(tmp) / "pack" / v
                d.mkdir(parents=True)
                (d / "manifest.json").write_text(json.dumps({"version": v}))

            versions = mgr.list_versions("pack")
            assert versions == ["1.0.0", "1.1.0", "2.0.0"]
