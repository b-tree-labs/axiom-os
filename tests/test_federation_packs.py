# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiompack format, lifecycle, and EC safety guard."""

from __future__ import annotations

import os
import tarfile
from pathlib import Path

import pytest

from axiom.vega.federation.packs import (
    PackManifest,
    check_ec_safety,
    create_pack,
    extract_pack,
    install_pack,
    list_installed_packs,
    remove_pack,
    verify_pack,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def content_dir(tmp_path: Path) -> Path:
    """Create a minimal content directory for pack creation."""
    d = tmp_path / "content"
    d.mkdir()
    (d / "readme.txt").write_text("hello world")
    (d / "data.csv").write_text("a,b,c\n1,2,3\n")
    sub = d / "sub"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested file")
    return d


@pytest.fixture()
def sample_pack(tmp_path: Path, content_dir: Path) -> Path:
    """Build a sample .axiompack archive."""
    return create_pack(
        pack_id="test-pack",
        version="0.1.0",
        content_type="rag",
        content_dir=content_dir,
        output=tmp_path / "test-pack-0.1.0.axiompack",
        description="A test pack",
        domain_tags=["testing"],
    )


@pytest.fixture()
def install_dir(tmp_path: Path) -> Path:
    d = tmp_path / "packs_store"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# PackManifest
# ---------------------------------------------------------------------------

class TestPackManifest:
    def test_to_dict_roundtrip(self):
        m = PackManifest(
            pack_id="demo",
            version="1.0.0",
            content_type="rag",
            description="A demo",
            domain_tags=["nuclear", "triga"],
        )
        d = m.to_dict()
        m2 = PackManifest.from_dict(d)
        assert m == m2

    def test_from_dict_ignores_unknown_keys(self):
        d = {"pack_id": "x", "version": "0.0.1", "content_type": "model", "unknown": 99}
        m = PackManifest.from_dict(d)
        assert m.pack_id == "x"
        assert not hasattr(m, "unknown")


# ---------------------------------------------------------------------------
# create_pack
# ---------------------------------------------------------------------------

class TestCreatePack:
    def test_produces_valid_tarball(self, sample_pack: Path):
        assert sample_pack.exists()
        assert sample_pack.suffix == ".axiompack"
        assert tarfile.is_tarfile(sample_pack)

    def test_tarball_contains_required_files(self, sample_pack: Path):
        with tarfile.open(sample_pack, "r:gz") as tar:
            names = tar.getnames()
        assert "manifest.json" in names
        assert "SHA256SUMS" in names

    def test_invalid_content_type_raises(self, content_dir: Path, tmp_path: Path):
        with pytest.raises(ValueError, match="Invalid content_type"):
            create_pack("x", "1.0.0", "bogus", content_dir, tmp_path / "out.axiompack")

    def test_invalid_access_tier_raises(self, content_dir: Path, tmp_path: Path):
        with pytest.raises(ValueError, match="Invalid access_tier"):
            create_pack(
                "x", "1.0.0", "rag", content_dir,
                tmp_path / "out.axiompack", access_tier="secret",
            )

    def test_missing_content_dir_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            create_pack("x", "1.0.0", "rag", tmp_path / "nonexistent")


# ---------------------------------------------------------------------------
# verify_pack
# ---------------------------------------------------------------------------

class TestVerifyPack:
    def test_valid_pack_passes(self, sample_pack: Path):
        assert verify_pack(sample_pack) is True

    def test_tampered_pack_fails(self, sample_pack: Path, tmp_path: Path):
        """Unpack, corrupt a file, repack, verify should fail."""
        extract_dir = tmp_path / "tampered"
        extract_pack(sample_pack, extract_dir)

        # Corrupt a content file
        (extract_dir / "readme.txt").write_text("CORRUPTED")

        # Re-tar without updating SHA256SUMS
        bad_pack = tmp_path / "bad.axiompack"
        with tarfile.open(bad_pack, "w:gz") as tar:
            for f in sorted(extract_dir.rglob("*")):
                if f.is_file():
                    tar.add(str(f), arcname=str(f.relative_to(extract_dir)))

        assert verify_pack(bad_pack) is False


# ---------------------------------------------------------------------------
# extract_pack
# ---------------------------------------------------------------------------

class TestExtractPack:
    def test_returns_correct_manifest(self, sample_pack: Path, tmp_path: Path):
        dest = tmp_path / "extracted"
        manifest = extract_pack(sample_pack, dest)
        assert manifest.pack_id == "test-pack"
        assert manifest.version == "0.1.0"
        assert manifest.content_type == "rag"
        assert manifest.description == "A test pack"

    def test_extracts_content_files(self, sample_pack: Path, tmp_path: Path):
        dest = tmp_path / "extracted"
        extract_pack(sample_pack, dest)
        assert (dest / "readme.txt").exists()
        assert (dest / "sub" / "nested.txt").exists()


# ---------------------------------------------------------------------------
# Round-trip: create → install → list → verify → remove
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_full_lifecycle(self, sample_pack: Path, install_dir: Path):
        # Install
        info = install_pack(sample_pack, install_dir=install_dir)
        assert info.installed is True
        assert info.manifest.pack_id == "test-pack"
        assert (install_dir / "test-pack" / "0.1.0" / "manifest.json").exists()

        # List
        packs = list_installed_packs(install_dir=install_dir)
        assert len(packs) == 1
        assert packs[0].manifest.pack_id == "test-pack"

        # Verify original archive
        assert verify_pack(sample_pack) is True

        # Remove
        assert remove_pack("test-pack", install_dir=install_dir) is True
        assert list_installed_packs(install_dir=install_dir) == []


# ---------------------------------------------------------------------------
# EC safety guard
# ---------------------------------------------------------------------------

class TestECSafety:
    def test_blocks_ec_locally(self):
        m = PackManifest(pack_id="secret", version="1.0.0", content_type="rag",
                         access_tier="export_controlled")
        # Ensure env var is NOT set
        env_backup = os.environ.pop("AXIOM_PRIVATECLOUD", None)
        try:
            assert check_ec_safety(m) is False
        finally:
            if env_backup is not None:
                os.environ["AXIOM_PRIVATECLOUD"] = env_backup

    def test_allows_ec_in_privatecloud(self, monkeypatch: pytest.MonkeyPatch):
        m = PackManifest(pack_id="secret", version="1.0.0", content_type="rag",
                         access_tier="export_controlled")
        monkeypatch.setenv("AXIOM_PRIVATECLOUD", "true")
        assert check_ec_safety(m) is True

    def test_allows_public_always(self):
        m = PackManifest(pack_id="pub", version="1.0.0", content_type="rag",
                         access_tier="public")
        assert check_ec_safety(m) is True

    def test_install_ec_raises(self, content_dir: Path, tmp_path: Path, install_dir: Path):
        pack = create_pack(
            "ec-pack", "1.0.0", "rag", content_dir,
            tmp_path / "ec.axiompack", access_tier="export_controlled",
        )
        env_backup = os.environ.pop("AXIOM_PRIVATECLOUD", None)
        try:
            with pytest.raises(PermissionError, match="export-controlled"):
                install_pack(pack, install_dir=install_dir)
        finally:
            if env_backup is not None:
                os.environ["AXIOM_PRIVATECLOUD"] = env_backup


# ---------------------------------------------------------------------------
# Idempotent install
# ---------------------------------------------------------------------------

class TestIdempotentInstall:
    def test_double_install_no_duplicate(self, sample_pack: Path, install_dir: Path):
        install_pack(sample_pack, install_dir=install_dir)
        install_pack(sample_pack, install_dir=install_dir)
        packs = list_installed_packs(install_dir=install_dir)
        assert len(packs) == 1


# ---------------------------------------------------------------------------
# Multiple versions
# ---------------------------------------------------------------------------

class TestMultipleVersions:
    def test_coexist(self, content_dir: Path, tmp_path: Path, install_dir: Path):
        p1 = create_pack("multi", "1.0.0", "rag", content_dir, tmp_path / "v1.axiompack")
        p2 = create_pack("multi", "2.0.0", "rag", content_dir, tmp_path / "v2.axiompack")
        install_pack(p1, install_dir=install_dir)
        install_pack(p2, install_dir=install_dir)

        packs = list_installed_packs(install_dir=install_dir)
        assert len(packs) == 2
        versions = {p.manifest.version for p in packs}
        assert versions == {"1.0.0", "2.0.0"}


# ---------------------------------------------------------------------------
# Remove specific version vs all
# ---------------------------------------------------------------------------

class TestRemovePack:
    def test_remove_specific_version(self, content_dir: Path, tmp_path: Path, install_dir: Path):
        p1 = create_pack("rm-test", "1.0.0", "rag", content_dir, tmp_path / "v1.axiompack")
        p2 = create_pack("rm-test", "2.0.0", "rag", content_dir, tmp_path / "v2.axiompack")
        install_pack(p1, install_dir=install_dir)
        install_pack(p2, install_dir=install_dir)

        assert remove_pack("rm-test", version="1.0.0", install_dir=install_dir) is True
        remaining = list_installed_packs(install_dir=install_dir)
        assert len(remaining) == 1
        assert remaining[0].manifest.version == "2.0.0"

    def test_remove_all_versions(self, content_dir: Path, tmp_path: Path, install_dir: Path):
        p1 = create_pack("rm-all", "1.0.0", "rag", content_dir, tmp_path / "v1.axiompack")
        p2 = create_pack("rm-all", "2.0.0", "rag", content_dir, tmp_path / "v2.axiompack")
        install_pack(p1, install_dir=install_dir)
        install_pack(p2, install_dir=install_dir)

        assert remove_pack("rm-all", install_dir=install_dir) is True
        assert list_installed_packs(install_dir=install_dir) == []

    def test_remove_nonexistent_returns_false(self, install_dir: Path):
        assert remove_pack("nonexistent", install_dir=install_dir) is False
