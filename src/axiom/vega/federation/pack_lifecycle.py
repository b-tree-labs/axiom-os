# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Pack lifecycle management — versioning, compatibility, rollback.

Manages the full lifecycle of data packs on a node:
- Version compatibility checking before install
- Previous version retention for rollback
- Active version tracking per pack
- Version listing and history

Usage::

    mgr = PackLifecycleManager()
    if check_pack_compatibility(pack_format="2.0.0", node_formats=["1.0.0","2.0.0"]):
        mgr.install(pack_path)
    mgr.rollback("community-knowledge", "1.0.0")
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Supported pack format versions for this node
SUPPORTED_FORMAT_VERSIONS = ["1.0.0", "2.0.0"]

_DEFAULT_INSTALL_DIR = Path.home() / ".axi" / "packs"
_ACTIVE_VERSION_FILE = ".active_version"


def check_pack_compatibility(
    pack_format: str,
    node_supported_formats: list[str] | None = None,
) -> bool:
    """Check if a pack's format version is compatible with this node.

    Args:
        pack_format: The pack's format_version string
        node_supported_formats: List of formats this node supports
            (default: SUPPORTED_FORMAT_VERSIONS)

    Returns:
        True if compatible, False if node cannot install this pack
    """
    supported = node_supported_formats or SUPPORTED_FORMAT_VERSIONS
    return pack_format in supported


class PackLifecycleManager:
    """Manages pack versions, rollback, and active version tracking."""

    def __init__(self, install_dir: Path | None = None) -> None:
        self._dir = install_dir or _DEFAULT_INSTALL_DIR

    def list_versions(self, pack_id: str) -> list[str]:
        """List installed versions of a pack, sorted ascending."""
        pack_dir = self._dir / pack_id
        if not pack_dir.exists():
            return []

        versions = []
        for ver_dir in pack_dir.iterdir():
            if ver_dir.is_dir() and (ver_dir / "manifest.json").exists():
                versions.append(ver_dir.name)

        return sorted(versions, key=_version_key)

    def get_active_version(self, pack_id: str) -> str | None:
        """Get the active (serving) version of a pack.

        Returns the explicitly set active version, or the latest installed.
        """
        active_file = self._dir / pack_id / _ACTIVE_VERSION_FILE
        if active_file.exists():
            return active_file.read_text(encoding="utf-8").strip()

        # Default: latest installed version
        versions = self.list_versions(pack_id)
        return versions[-1] if versions else None

    def set_active_version(self, pack_id: str, version: str) -> None:
        """Set the active version for a pack."""
        active_file = self._dir / pack_id / _ACTIVE_VERSION_FILE
        active_file.parent.mkdir(parents=True, exist_ok=True)
        active_file.write_text(version, encoding="utf-8")
        log.info("Set active version for %s to %s", pack_id, version)

    def rollback(self, pack_id: str, target_version: str) -> bool:
        """Rollback to a previous version.

        Args:
            pack_id: Pack identifier
            target_version: Version to rollback to (must be installed)

        Returns:
            True if rollback succeeded
        """
        versions = self.list_versions(pack_id)
        if target_version not in versions:
            log.error(
                "Cannot rollback %s to %s — not installed. Available: %s",
                pack_id,
                target_version,
                versions,
            )
            return False

        self.set_active_version(pack_id, target_version)
        log.info("Rolled back %s to %s", pack_id, target_version)
        return True

    def get_manifest(self, pack_id: str, version: str | None = None) -> dict | None:
        """Read the manifest for a specific pack version."""
        if version is None:
            version = self.get_active_version(pack_id)
        if version is None:
            return None

        manifest_path = self._dir / pack_id / version / "manifest.json"
        if not manifest_path.exists():
            return None

        return json.loads(manifest_path.read_text(encoding="utf-8"))

    def prune(self, pack_id: str, keep_versions: int = 3) -> list[str]:
        """Remove old versions, keeping the N most recent + active.

        Returns list of pruned version strings.
        """
        import shutil

        versions = self.list_versions(pack_id)
        active = self.get_active_version(pack_id)

        # Always keep active version
        to_keep = set()
        if active:
            to_keep.add(active)

        # Keep the N most recent
        for v in versions[-keep_versions:]:
            to_keep.add(v)

        pruned = []
        for v in versions:
            if v not in to_keep:
                ver_dir = self._dir / pack_id / v
                shutil.rmtree(ver_dir, ignore_errors=True)
                pruned.append(v)
                log.info("Pruned %s version %s", pack_id, v)

        return pruned


def _version_key(version: str) -> tuple:
    """Sort key for semver strings."""
    parts = version.split(".")
    result = []
    for p in parts:
        try:
            result.append(int(p))
        except ValueError:
            result.append(0)
    return tuple(result)
