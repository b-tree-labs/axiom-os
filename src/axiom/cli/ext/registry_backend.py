# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Local-filesystem registry backend for Phase 3 ``publish`` and Phase 4 verbs.

Storage layout under ``$AXIOM_HOME/registry/`` (default
``~/.axiom/registry/``)::

    registry/
      index.json                          { "schema_version": 1, "extensions": {
                                              "<name>": {"latest": "X.Y.Z",
                                                          "versions": ["X.Y.Z", ...]}
                                          }}
      <name>/<version>/manifest.toml
      <name>/<version>/<name>-<version>.tar.gz
      <name>/<version>/<name>-<version>.tar.gz.sig     # detached hex ed25519
      <name>/<version>/attestation.json

All mutating operations are atomic: writes land in a sibling tempfile and are
``os.replace``-d into place. The index file in particular is never overwritten
in a way that could leave a half-parsed blob after a crash.

Env overrides:

- ``AXIOM_REGISTRY_URL`` — replaces the default root. Only ``file://`` URLs
  are supported at v0.1; anything else raises :class:`ValueError`. A Vyzier
  remote registry plugs in later as an entry-point override of
  :mod:`axiom.cli.ext.commands.publish` (and friends).
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom.cli.ext.commands.config import _axiom_home

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

# ``schema_version`` for ``index.json``. Bump when the on-disk layout changes;
# readers that see an unexpected value should refuse to proceed.
_INDEX_SCHEMA_VERSION = 1

# Names forbidden in a <name>/<version> path — conservative guard against path
# traversal. We want both components to be plain relative segments.
_BAD_PATH_TOKENS: frozenset[str] = frozenset({"", ".", ".."})


def _validate_path_component(label: str, value: str) -> None:
    """Reject anything that could escape the registry root.

    ``label`` is the display name ("name" / "version") used in error messages.
    """
    if not isinstance(value, str) or value in _BAD_PATH_TOKENS:
        raise ValueError(f"invalid {label}: {value!r}")
    if "/" in value or "\\" in value or value.startswith(".."):
        raise ValueError(f"invalid {label} (path traversal): {value!r}")
    if os.sep in value:
        raise ValueError(f"invalid {label} (separator): {value!r}")


def registry_root() -> Path:
    """Return the filesystem root for the active registry.

    Resolution order:

    1. ``AXIOM_REGISTRY_URL`` env var, when present. Must be ``file://``.
    2. ``$AXIOM_HOME/registry`` (default ``~/.axiom/registry``).
    """
    override = os.environ.get("AXIOM_REGISTRY_URL")
    if override:
        # We deliberately don't use urllib.parse here — the only valid scheme
        # is ``file://`` and the path after it is a literal filesystem path.
        if not override.startswith("file://"):
            raise ValueError(
                "AXIOM_REGISTRY_URL must use the file:// scheme at v0.1 "
                f"(got {override!r}); remote registries plug in as provider "
                "overrides later."
            )
        return Path(override[len("file://"):])
    return _axiom_home() / "registry"


@dataclass(frozen=True)
class RegistryPath:
    """Bundle of filesystem paths derived from :func:`registry_root`.

    Use :meth:`resolve` to build the default one; the class is also handy as
    a typed container inside higher-level code that passes paths around.
    """

    root: Path

    @classmethod
    def resolve(cls) -> RegistryPath:
        return cls(root=registry_root())

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def extension_dir(self, name: str) -> Path:
        _validate_path_component("name", name)
        return self.root / name

    def version_dir(self, name: str, version: str) -> Path:
        _validate_path_component("name", name)
        _validate_path_component("version", version)
        return self.root / name / version


# ---------------------------------------------------------------------------
# Artifact records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactRecord:
    """A single published extension artifact — all four on-disk files."""

    name: str
    version: str
    manifest_path: Path
    artifact_path: Path
    sig_path: Path
    attestation: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Index helpers
# ---------------------------------------------------------------------------


def _seed_index() -> dict[str, Any]:
    return {"schema_version": _INDEX_SCHEMA_VERSION, "extensions": {}}


def read_index() -> dict[str, Any]:
    """Return the parsed ``index.json`` or the seed for an empty registry."""
    rp = RegistryPath.resolve()
    if not rp.index_path.exists():
        return _seed_index()
    try:
        data = json.loads(rp.index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A corrupt index is indistinguishable from "no index" for read purposes;
        # the caller who writes next will overwrite it atomically.
        return _seed_index()
    if not isinstance(data, dict) or "extensions" not in data:
        return _seed_index()
    # Normalize the schema version if older/newer — for v0.1 we only know v1.
    if data.get("schema_version") != _INDEX_SCHEMA_VERSION:
        raise ValueError(
            f"registry index.json has unsupported schema_version "
            f"{data.get('schema_version')!r}; expected {_INDEX_SCHEMA_VERSION}"
        )
    return data


def write_index(data: dict[str, Any]) -> None:
    """Atomically overwrite the registry index.

    The write goes to a sibling temp file under the same parent; a successful
    ``os.replace`` swaps it into place. If the rename fails, the existing
    index is preserved untouched.
    """
    rp = RegistryPath.resolve()
    rp.root.mkdir(parents=True, exist_ok=True)
    tmp_name = f"index.json.{uuid.uuid4().hex}.tmp"
    tmp_path = rp.root / tmp_name
    try:
        tmp_path.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(tmp_path, rp.index_path)
    except Exception:
        # Best-effort cleanup; do not mask the original error.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------


def list_extensions() -> list[str]:
    """Return all extension names currently indexed."""
    idx = read_index()
    return sorted(idx.get("extensions", {}).keys())


def list_versions(name: str) -> list[str]:
    """Return versions for ``name`` in registration order (index-preserved)."""
    idx = read_index()
    entry = idx.get("extensions", {}).get(name) or {}
    versions = entry.get("versions") or []
    return list(versions)


def get(name: str, version: str) -> ArtifactRecord | None:
    """Return the :class:`ArtifactRecord` for ``(name, version)`` or ``None``.

    The returned record points at the on-disk files — it does not copy them.
    """
    try:
        rp = RegistryPath.resolve()
        vdir = rp.version_dir(name, version)
    except ValueError:
        return None
    if not vdir.is_dir():
        return None
    manifest_path = vdir / "manifest.toml"
    artifact_path = vdir / f"{name}-{version}.tar.gz"
    sig_path = vdir / f"{name}-{version}.tar.gz.sig"
    att_path = vdir / "attestation.json"
    if not manifest_path.exists() or not artifact_path.exists():
        return None
    attestation: dict[str, Any] = {}
    if att_path.exists():
        try:
            attestation = json.loads(att_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            attestation = {}
    return ArtifactRecord(
        name=name,
        version=version,
        manifest_path=manifest_path,
        artifact_path=artifact_path,
        sig_path=sig_path,
        attestation=attestation,
    )


# ---------------------------------------------------------------------------
# Write API
# ---------------------------------------------------------------------------


def _version_key(version: str) -> tuple:
    """Loose semver ordering for "latest" selection.

    We don't want a full PEP 440 / SemVer implementation here. Splitting on
    dots and falling back to the raw string for non-numeric parts is enough
    for the v0.1 use case where registry versions are well-formed.
    """
    parts: list[tuple[int, Any]] = []
    for segment in version.split("."):
        if segment.isdigit():
            parts.append((0, int(segment)))
        else:
            parts.append((1, segment))
    return tuple(parts)


def put(
    name: str,
    version: str,
    manifest_path: Path,
    artifact_path: Path,
    sig_path: Path,
    attestation: dict[str, Any],
) -> ArtifactRecord:
    """Store a published artifact and update the index atomically.

    Order of operations:

    1. Validate path components (reject traversal).
    2. Stage file copies into a sibling temp directory.
    3. Atomically swap the staged directory into
       ``registry/<name>/<version>/``.
    4. Rewrite the index via :func:`write_index` (also atomic).

    The index rewrite is the last step — if it fails, the version directory
    is still present but invisible to ``list_versions`` / ``get`` (since the
    index is the source of truth). Callers observing an error should retry
    or call ``remove(name, version)`` to clean up; future callers that
    rewrite the index will pick the staged directory back up.
    """
    _validate_path_component("name", name)
    _validate_path_component("version", version)

    rp = RegistryPath.resolve()
    vdir = rp.version_dir(name, version)
    vdir.parent.mkdir(parents=True, exist_ok=True)

    # Stage into a temp directory next to the target, then rename.
    staged = vdir.parent / f"{version}.staging.{uuid.uuid4().hex}"
    staged.mkdir()
    try:
        staged_manifest = staged / "manifest.toml"
        shutil.copy2(manifest_path, staged_manifest)
        staged_artifact = staged / f"{name}-{version}.tar.gz"
        shutil.copy2(artifact_path, staged_artifact)
        staged_sig = staged / f"{name}-{version}.tar.gz.sig"
        shutil.copy2(sig_path, staged_sig)
        staged_att = staged / "attestation.json"
        staged_att.write_text(
            json.dumps(attestation, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        # If a previous version dir exists (re-publish), replace it.
        if vdir.exists():
            shutil.rmtree(vdir)
        os.replace(staged, vdir)
    except Exception:
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        raise

    # Now update the index — also atomic.
    idx = read_index()
    ext_table = idx.setdefault("extensions", {})
    entry = ext_table.setdefault(name, {"latest": version, "versions": []})
    versions = entry.setdefault("versions", [])
    if version not in versions:
        versions.append(version)
    # Recompute latest by our loose semver comparator.
    entry["latest"] = max(versions, key=_version_key)
    try:
        write_index(idx)
    except Exception:
        # If the index write fails, keep the on-disk artifact — but surface
        # the error to the caller, which may choose to call ``remove``.
        raise

    return ArtifactRecord(
        name=name,
        version=version,
        manifest_path=vdir / "manifest.toml",
        artifact_path=vdir / f"{name}-{version}.tar.gz",
        sig_path=vdir / f"{name}-{version}.tar.gz.sig",
        attestation=attestation,
    )


def remove(name: str, version: str) -> None:
    """Remove a published version. Idempotent.

    Deletes ``registry/<name>/<version>/`` and trims the index. If no
    versions remain for ``name``, the extension entry is removed from the
    index (but the empty ``registry/<name>/`` directory is left for inspection
    if something went wrong — it's harmless and a future ``put`` repopulates
    it).
    """
    _validate_path_component("name", name)
    _validate_path_component("version", version)

    rp = RegistryPath.resolve()
    vdir = rp.version_dir(name, version)
    if vdir.exists():
        shutil.rmtree(vdir)

    idx = read_index()
    ext_table = idx.get("extensions", {})
    entry = ext_table.get(name)
    if entry is None:
        return
    versions = [v for v in entry.get("versions", []) if v != version]
    if not versions:
        ext_table.pop(name, None)
    else:
        entry["versions"] = versions
        entry["latest"] = max(versions, key=_version_key)
    idx["extensions"] = ext_table
    write_index(idx)


__all__ = [
    "ArtifactRecord",
    "RegistryPath",
    "get",
    "list_extensions",
    "list_versions",
    "put",
    "read_index",
    "registry_root",
    "remove",
    "write_index",
]
