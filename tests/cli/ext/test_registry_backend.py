# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for the local-filesystem registry backend.

The backend is the persistence layer that Phase 3 (``publish``) and Phase 4
(``install``, ``search``, etc.) both consume. The Vyzier remote registry is
a later provider override — for v0.1 we only speak ``file://`` URLs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.cli.ext.registry_backend import (
    ArtifactRecord,
    RegistryPath,
    get,
    list_extensions,
    list_versions,
    put,
    read_index,
    registry_root,
    remove,
    write_index,
)


@pytest.fixture
def axiom_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axiom_home"
    home.mkdir()
    monkeypatch.setenv("AXIOM_HOME", str(home))
    # Ensure no stale override leaks across tests.
    monkeypatch.delenv("AXIOM_REGISTRY_URL", raising=False)
    return home


def _write_ext_artifacts(
    tmp_path: Path, name: str = "foo", version: str = "0.1.0"
) -> tuple[Path, Path, Path, dict]:
    """Create a fake manifest + tarball + signature trio in ``tmp_path``."""
    stage = tmp_path / f"stage-{name}-{version}"
    stage.mkdir()
    manifest = stage / "axiom-extension.toml"
    manifest.write_text(
        f'[extension]\nname = "{name}"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    artifact = stage / f"{name}-{version}.tar.gz"
    artifact.write_bytes(b"fake-tarball-contents")
    sig = stage / f"{name}-{version}.tar.gz.sig"
    sig.write_text("deadbeef", encoding="utf-8")
    attestation = {
        "publisher": "b-tree-labs",
        "published_at": "2026-04-22T00:00:00Z",
        "artifact_sha256": "deadbeef",
        "sig_algo": "ed25519",
        "public_key_sha256": "cafebabe",
    }
    return manifest, artifact, sig, attestation


# ---------------------------------------------------------------------------
# RegistryPath resolution
# ---------------------------------------------------------------------------


def test_registry_root_defaults_to_axiom_home_subdir(axiom_home: Path) -> None:
    root = registry_root()
    assert root == axiom_home / "registry"


def test_registry_root_honors_axiom_registry_url_file_scheme(
    axiom_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "alt-registry"
    override.mkdir()
    monkeypatch.setenv("AXIOM_REGISTRY_URL", f"file://{override}")
    assert registry_root() == override


def test_registry_root_rejects_non_file_scheme(
    axiom_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AXIOM_REGISTRY_URL", "https://example.com/registry")
    with pytest.raises(ValueError, match="file://"):
        registry_root()


def test_registry_path_dataclass_exposes_subpaths(axiom_home: Path) -> None:
    path = RegistryPath.resolve()
    assert path.root == axiom_home / "registry"
    assert path.index_path == axiom_home / "registry" / "index.json"
    assert path.extension_dir("foo") == axiom_home / "registry" / "foo"
    assert (
        path.version_dir("foo", "1.2.3")
        == axiom_home / "registry" / "foo" / "1.2.3"
    )


# ---------------------------------------------------------------------------
# Index I/O
# ---------------------------------------------------------------------------


def test_read_index_on_empty_registry_returns_seed(axiom_home: Path) -> None:
    idx = read_index()
    assert idx == {"schema_version": 1, "extensions": {}}


def test_write_index_round_trips(axiom_home: Path) -> None:
    write_index(
        {
            "schema_version": 1,
            "extensions": {"foo": {"latest": "0.1.0", "versions": ["0.1.0"]}},
        }
    )
    idx = read_index()
    assert idx["extensions"]["foo"]["latest"] == "0.1.0"


def test_index_file_is_pinned_schema_version(
    axiom_home: Path, tmp_path: Path
) -> None:
    manifest, artifact, sig, attestation = _write_ext_artifacts(tmp_path)
    put("foo", "0.1.0", manifest, artifact, sig, attestation)
    data = json.loads((axiom_home / "registry" / "index.json").read_text())
    assert data["schema_version"] == 1
    assert "extensions" in data


# ---------------------------------------------------------------------------
# put / get / list roundtrip
# ---------------------------------------------------------------------------


def test_put_then_get_returns_artifact_record(
    axiom_home: Path, tmp_path: Path
) -> None:
    manifest, artifact, sig, attestation = _write_ext_artifacts(tmp_path)
    record = put("foo", "0.1.0", manifest, artifact, sig, attestation)
    assert isinstance(record, ArtifactRecord)
    assert record.name == "foo"
    assert record.version == "0.1.0"
    assert record.manifest_path.exists()
    assert record.artifact_path.exists()
    assert record.sig_path.exists()
    assert record.attestation["publisher"] == "b-tree-labs"

    fetched = get("foo", "0.1.0")
    assert fetched is not None
    assert fetched.name == "foo"
    assert fetched.artifact_path.read_bytes() == b"fake-tarball-contents"


def test_get_missing_returns_none(axiom_home: Path) -> None:
    assert get("nothing", "0.0.1") is None


def test_list_extensions_and_versions(
    axiom_home: Path, tmp_path: Path
) -> None:
    m1, a1, s1, att1 = _write_ext_artifacts(tmp_path, "foo", "0.1.0")
    put("foo", "0.1.0", m1, a1, s1, att1)
    m2, a2, s2, att2 = _write_ext_artifacts(tmp_path, "foo", "0.2.0")
    put("foo", "0.2.0", m2, a2, s2, att2)
    m3, a3, s3, att3 = _write_ext_artifacts(tmp_path, "bar", "1.0.0")
    put("bar", "1.0.0", m3, a3, s3, att3)

    assert sorted(list_extensions()) == ["bar", "foo"]
    assert sorted(list_versions("foo")) == ["0.1.0", "0.2.0"]
    assert list_versions("bar") == ["1.0.0"]


def test_put_updates_latest_to_highest_version(
    axiom_home: Path, tmp_path: Path
) -> None:
    m1, a1, s1, att1 = _write_ext_artifacts(tmp_path, "foo", "0.1.0")
    put("foo", "0.1.0", m1, a1, s1, att1)
    m2, a2, s2, att2 = _write_ext_artifacts(tmp_path, "foo", "0.2.0")
    put("foo", "0.2.0", m2, a2, s2, att2)

    idx = read_index()
    assert idx["extensions"]["foo"]["latest"] == "0.2.0"
    assert sorted(idx["extensions"]["foo"]["versions"]) == ["0.1.0", "0.2.0"]


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


def test_corrupted_index_midwrite_does_not_leave_half_state(
    axiom_home: Path, tmp_path: Path
) -> None:
    """Simulate a crash between tempfile-write and rename.

    After a simulated crash we should still be able to ``read_index()`` and
    get the *previous* valid index (or the seed), never a partial JSON.
    """
    import axiom.cli.ext.registry_backend as backend

    # First, land a good version.
    m1, a1, s1, att1 = _write_ext_artifacts(tmp_path, "foo", "0.1.0")
    put("foo", "0.1.0", m1, a1, s1, att1)
    pre_idx = read_index()

    # Now poison os.replace so the atomic rename fails — but only for the
    # index swap. We wrap in try/finally so we can restore without disturbing
    # the surrounding axiom_home fixture's monkeypatch (which owns AXIOM_HOME).
    real_replace = backend.os.replace

    def exploding_replace(src, dst):  # noqa: ANN001
        if Path(dst).name == "index.json":
            raise OSError("simulated crash between write and rename")
        return real_replace(src, dst)

    backend.os.replace = exploding_replace  # type: ignore[assignment]
    try:
        m2, a2, s2, att2 = _write_ext_artifacts(tmp_path, "foo", "0.2.0")
        with pytest.raises(OSError, match="simulated crash"):
            put("foo", "0.2.0", m2, a2, s2, att2)
    finally:
        backend.os.replace = real_replace  # type: ignore[assignment]

    # The existing index must still be the pre-crash one.
    post_idx = read_index()
    assert post_idx == pre_idx


def test_put_does_not_leak_tempfile_on_success(
    axiom_home: Path, tmp_path: Path
) -> None:
    manifest, artifact, sig, attestation = _write_ext_artifacts(tmp_path)
    put("foo", "0.1.0", manifest, artifact, sig, attestation)
    leftovers = list((axiom_home / "registry").glob("*.tmp"))
    assert leftovers == []


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_remove_trims_version_but_keeps_extension_if_others_remain(
    axiom_home: Path, tmp_path: Path
) -> None:
    m1, a1, s1, att1 = _write_ext_artifacts(tmp_path, "foo", "0.1.0")
    put("foo", "0.1.0", m1, a1, s1, att1)
    m2, a2, s2, att2 = _write_ext_artifacts(tmp_path, "foo", "0.2.0")
    put("foo", "0.2.0", m2, a2, s2, att2)

    remove("foo", "0.1.0")
    assert list_versions("foo") == ["0.2.0"]
    assert "foo" in list_extensions()
    # Pruned version directory is gone.
    assert not (axiom_home / "registry" / "foo" / "0.1.0").exists()


def test_remove_drops_extension_entry_when_last_version_removed(
    axiom_home: Path, tmp_path: Path
) -> None:
    manifest, artifact, sig, attestation = _write_ext_artifacts(tmp_path)
    put("foo", "0.1.0", manifest, artifact, sig, attestation)
    remove("foo", "0.1.0")
    idx = read_index()
    assert "foo" not in idx["extensions"]


def test_remove_unknown_version_is_idempotent(
    axiom_home: Path, tmp_path: Path
) -> None:
    manifest, artifact, sig, attestation = _write_ext_artifacts(tmp_path)
    put("foo", "0.1.0", manifest, artifact, sig, attestation)
    # Removing a non-existent version should not error.
    remove("foo", "9.9.9")
    assert list_versions("foo") == ["0.1.0"]


# ---------------------------------------------------------------------------
# Path traversal refusal
# ---------------------------------------------------------------------------


def test_put_rejects_path_traversal_in_name(
    axiom_home: Path, tmp_path: Path
) -> None:
    manifest, artifact, sig, attestation = _write_ext_artifacts(tmp_path)
    with pytest.raises(ValueError, match="invalid"):
        put("../evil", "0.1.0", manifest, artifact, sig, attestation)


def test_put_rejects_path_traversal_in_version(
    axiom_home: Path, tmp_path: Path
) -> None:
    manifest, artifact, sig, attestation = _write_ext_artifacts(tmp_path)
    with pytest.raises(ValueError, match="invalid"):
        put("foo", "../0.1.0", manifest, artifact, sig, attestation)
