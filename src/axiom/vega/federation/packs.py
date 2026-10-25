# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Axiompack format — portable knowledge and content packs.

The .axiompack format is a gzip-compressed tar archive containing:
  - manifest.json   — pack metadata
  - SHA256SUMS      — integrity checksums for every content file
  - content files    — chunks.parquet (RAG packs) or arbitrary files
"""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

PACK_EXTENSION = ".axiompack"
DEFAULT_INSTALL_DIR = Path.home() / ".axi" / "packs"

VALID_CONTENT_TYPES = frozenset({"rag", "materials", "facility", "model"})
VALID_ACCESS_TIERS = frozenset({"public", "restricted", "export_controlled"})


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PackManifest:
    """Metadata stored inside every .axiompack archive as manifest.json."""

    pack_id: str
    version: str
    content_type: str
    description: str = ""
    access_tier: str = "public"
    created_at: str = ""
    created_by: str = ""
    domain_tags: list[str] = field(default_factory=list)
    chunk_count: int = 0
    source_node: str = ""
    dependencies: list[str] = field(default_factory=list)
    format_version: str = "1.0.0"
    compatible_axiom_versions: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> PackManifest:
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class PackInfo:
    """Lightweight handle to an installed or discovered pack."""

    manifest: PackManifest
    path: Path
    checksum: str = ""
    installed: bool = False

    def to_dict(self) -> dict:
        return {
            "manifest": self.manifest.to_dict(),
            "path": str(self.path),
            "checksum": self.checksum,
            "installed": self.installed,
        }


# ---------------------------------------------------------------------------
# EC safety guard
# ---------------------------------------------------------------------------


def check_ec_safety(manifest: PackManifest) -> bool:
    """Enforce: export-controlled packs NEVER install locally.

    Returns True if safe to install, False if blocked.
    Non-negotiable invariant from spec.
    """
    if manifest.access_tier == "export_controlled":
        if os.environ.get("AXIOM_PRIVATECLOUD") != "true":
            return False
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _collect_files(directory: Path) -> list[Path]:
    """Return sorted list of files relative to *directory*."""
    return sorted(p.relative_to(directory) for p in directory.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_pack(
    pack_id: str,
    version: str,
    content_type: str,
    content_dir: Path,
    output: Path | None = None,
    access_tier: str = "public",
    description: str = "",
    domain_tags: list[str] | None = None,
) -> Path:
    """Create a .axiompack archive from a content directory.

    Parameters
    ----------
    pack_id:
        Lowercase hyphen-separated identifier.
    version:
        Semver string (e.g. "1.0.0").
    content_type:
        One of "rag", "materials", "facility", "model".
    content_dir:
        Directory whose contents become the pack payload.
    output:
        Destination path for the archive.  Defaults to
        ``{cwd}/{pack_id}-{version}.axiompack``.
    access_tier:
        "public", "restricted", or "export_controlled".
    description:
        Human-readable description.
    domain_tags:
        Optional list of domain tags.

    Returns
    -------
    Path to the created archive.
    """
    content_dir = Path(content_dir)
    if not content_dir.is_dir():
        msg = f"content_dir is not a directory: {content_dir}"
        raise FileNotFoundError(msg)

    if content_type not in VALID_CONTENT_TYPES:
        msg = f"Invalid content_type '{content_type}'; expected one of {VALID_CONTENT_TYPES}"
        raise ValueError(msg)

    if access_tier not in VALID_ACCESS_TIERS:
        msg = f"Invalid access_tier '{access_tier}'; expected one of {VALID_ACCESS_TIERS}"
        raise ValueError(msg)

    manifest = PackManifest(
        pack_id=pack_id,
        version=version,
        content_type=content_type,
        description=description,
        access_tier=access_tier,
        created_at=datetime.now(UTC).isoformat(),
        domain_tags=domain_tags or [],
    )

    if output is None:
        output = Path.cwd() / f"{pack_id}-{version}{PACK_EXTENSION}"
    output = Path(output)

    with tempfile.TemporaryDirectory() as staging:
        staging_path = Path(staging)

        # Copy content files into staging
        content_files = _collect_files(content_dir)
        for rel in content_files:
            dest = staging_path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((content_dir / rel).read_bytes())

        # Count chunks if RAG pack with parquet
        if content_type == "rag":
            parquet = staging_path / "chunks.parquet"
            if parquet.exists():
                # Simple heuristic — actual count would need pyarrow
                manifest.chunk_count = -1  # sentinel; reader can update

        # Write manifest
        manifest_path = staging_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest.to_dict(), indent=2))

        # Compute SHA256SUMS (manifest + content)
        all_files = _collect_files(staging_path)
        sums_lines: list[str] = []
        for rel in all_files:
            digest = _sha256_file(staging_path / rel)
            sums_lines.append(f"{digest}  {rel}")
        sums_path = staging_path / "SHA256SUMS"
        sums_path.write_text("\n".join(sums_lines) + "\n")

        # Build tar.gz
        output.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output, "w:gz") as tar:
            # Add SHA256SUMS first, then manifest, then content
            tar.add(str(sums_path), arcname="SHA256SUMS")
            tar.add(str(manifest_path), arcname="manifest.json")
            for rel in content_files:
                tar.add(str(staging_path / rel), arcname=str(rel))

    return output


def extract_pack(archive_path: Path, dest: Path) -> PackManifest:
    """Extract a .axiompack and return its manifest."""
    archive_path = Path(archive_path)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(dest, filter="data")

    manifest_path = dest / "manifest.json"
    if not manifest_path.exists():
        msg = f"No manifest.json in {archive_path}"
        raise ValueError(msg)

    data = json.loads(manifest_path.read_text())
    return PackManifest.from_dict(data)


def verify_pack(archive_path: Path) -> bool:
    """Verify SHA256SUMS inside a pack archive.

    Returns True if all checksums match, False otherwise.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(tmp_path, filter="data")

        sums_file = tmp_path / "SHA256SUMS"
        if not sums_file.exists():
            return False

        for line in sums_file.read_text().strip().splitlines():
            expected, _, rel_str = line.partition("  ")
            file_path = tmp_path / rel_str
            if not file_path.exists():
                return False
            if _sha256_file(file_path) != expected:
                return False

    return True


def install_pack(
    archive_path: Path,
    install_dir: Path | None = None,
) -> PackInfo:
    """Install a pack to the local pack store (~/.axi/packs/).

    Idempotent: re-installing the same pack+version overwrites in place.
    """
    archive_path = Path(archive_path)
    install_dir = Path(install_dir) if install_dir else DEFAULT_INSTALL_DIR

    # Extract to temp first to read manifest
    with tempfile.TemporaryDirectory() as tmp:
        manifest = extract_pack(archive_path, Path(tmp))

        if not check_ec_safety(manifest):
            msg = (
                f"Pack '{manifest.pack_id}' is export-controlled and cannot "
                "be installed outside a PrivateCloud environment."
            )
            raise PermissionError(msg)

        dest = install_dir / manifest.pack_id / manifest.version
        if dest.exists():
            import shutil

            shutil.rmtree(dest)

        dest.mkdir(parents=True, exist_ok=True)

        # Move extracted files
        tmp_path = Path(tmp)
        for item in tmp_path.iterdir():
            target = dest / item.name
            if item.is_dir():
                import shutil

                shutil.copytree(item, target)
            else:
                import shutil

                shutil.copy2(item, target)

    checksum = _sha256_file(archive_path)

    return PackInfo(
        manifest=manifest,
        path=dest,
        checksum=checksum,
        installed=True,
    )


def list_installed_packs(install_dir: Path | None = None) -> list[PackInfo]:
    """List all installed packs."""
    install_dir = Path(install_dir) if install_dir else DEFAULT_INSTALL_DIR

    if not install_dir.exists():
        return []

    packs: list[PackInfo] = []
    for pack_dir in sorted(install_dir.iterdir()):
        if not pack_dir.is_dir():
            continue
        for ver_dir in sorted(pack_dir.iterdir()):
            if not ver_dir.is_dir():
                continue
            manifest_path = ver_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            data = json.loads(manifest_path.read_text())
            manifest = PackManifest.from_dict(data)
            packs.append(PackInfo(manifest=manifest, path=ver_dir, installed=True))

    return packs


def remove_pack(
    pack_id: str,
    version: str | None = None,
    install_dir: Path | None = None,
) -> bool:
    """Remove an installed pack.

    If *version* is None, removes all versions.
    Returns True if something was removed, False otherwise.
    """
    import shutil

    install_dir = Path(install_dir) if install_dir else DEFAULT_INSTALL_DIR
    pack_dir = install_dir / pack_id

    if not pack_dir.exists():
        return False

    if version is None:
        shutil.rmtree(pack_dir)
        return True

    ver_dir = pack_dir / version
    if not ver_dir.exists():
        return False

    shutil.rmtree(ver_dir)

    # Clean up empty pack directory
    if not any(pack_dir.iterdir()):
        pack_dir.rmdir()

    return True
