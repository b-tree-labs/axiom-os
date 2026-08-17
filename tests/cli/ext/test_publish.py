# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext publish`` — author-side build + scan + sign + registry.

The publish flow is the union of Units 2 (scan), 3 (sign), and 1 (registry
backend). Happy paths here lean on the registry backend to verify that a
published artifact is in fact retrievable via ``get(name, version)``.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from axiom.cli.ext.commands.publish import PublishProvider, publish_extension
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.registry_backend import get as registry_get
from axiom.cli.ext.registry_backend import list_extensions, list_versions


@pytest.fixture
def axiom_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axiom_home"
    home.mkdir()
    monkeypatch.setenv("AXIOM_HOME", str(home))
    monkeypatch.delenv("AXIOM_REGISTRY_URL", raising=False)
    return home


@pytest.fixture
def scaffolded_publishable(scaffolded_extension):
    """Scaffold a fresh extension for publish flow tests."""

    def _scaffold(name: str = "pub_ext") -> Path:
        return scaffolded_extension(name)

    return _scaffold


def _run_publish_cli(
    ext_path: Path, *argv: str, capsys=None
) -> tuple[int, str]:
    provider = PublishProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args([str(ext_path), "--yes", *argv])
    ctx = CliContext(cwd=ext_path.parent)
    if capsys is not None:
        capsys.readouterr()
    rc = provider.run(args, ctx)
    out = capsys.readouterr().out if capsys is not None else ""
    return rc, out


# ---------------------------------------------------------------------------
# Provider registration
# ---------------------------------------------------------------------------


def test_publish_is_registered_as_builtin() -> None:
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "publish" in providers
    assert providers["publish"].verb == "publish"


# ---------------------------------------------------------------------------
# Happy path — end-to-end
# ---------------------------------------------------------------------------


def test_publish_lands_artifact_in_registry(
    scaffolded_publishable, axiom_home: Path, capsys
) -> None:
    ext = scaffolded_publishable("happy_ext")
    rc, out = _run_publish_cli(
        ext, "--no-tag-check", capsys=capsys
    )
    assert rc == 0, out
    assert "Published" in out or "published" in out.lower()
    assert "happy_ext" in list_extensions()
    assert "0.1.0" in list_versions("happy_ext")

    record = registry_get("happy_ext", "0.1.0")
    assert record is not None
    assert record.manifest_path.exists()
    assert record.artifact_path.exists()
    assert record.sig_path.exists()
    assert record.attestation.get("publisher")


def test_publish_direct_api_returns_record(
    scaffolded_publishable, axiom_home: Path
) -> None:
    ext = scaffolded_publishable("direct_pub_ext")
    record = publish_extension(
        ext,
        yes=True,
        skip_tag_check=True,
    )
    assert record.name == "direct_pub_ext"
    assert record.version == "0.1.0"


# ---------------------------------------------------------------------------
# Re-publish guarding
# ---------------------------------------------------------------------------


def test_publish_refuses_to_overwrite_existing_version(
    scaffolded_publishable, axiom_home: Path, capsys
) -> None:
    ext = scaffolded_publishable("reup_ext")
    rc, _ = _run_publish_cli(ext, "--no-tag-check", capsys=capsys)
    assert rc == 0
    rc, out = _run_publish_cli(ext, "--no-tag-check", capsys=capsys)
    assert rc == 1
    assert "overwrite" in out.lower() or "already" in out.lower()


def test_publish_overwrite_flag_allows_republish(
    scaffolded_publishable, axiom_home: Path, capsys
) -> None:
    ext = scaffolded_publishable("allow_ext")
    rc, _ = _run_publish_cli(ext, "--no-tag-check", capsys=capsys)
    assert rc == 0
    rc, out = _run_publish_cli(
        ext, "--no-tag-check", "--allow-overwrite", capsys=capsys
    )
    assert rc == 0, out


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


def test_publish_dry_run_does_not_touch_registry(
    scaffolded_publishable, axiom_home: Path, capsys
) -> None:
    ext = scaffolded_publishable("dry_ext")
    rc, out = _run_publish_cli(
        ext, "--no-tag-check", "--dry-run", capsys=capsys
    )
    assert rc == 0, out
    assert "dry" in out.lower()
    assert "dry_ext" not in list_extensions()
    # But a local artifact + sig should exist in the extension's dist/.
    dist = ext / "dist"
    assert dist.exists()
    assert (dist / "dry_ext-0.1.0.tar.gz").exists()
    assert (dist / "dry_ext-0.1.0.tar.gz.sig").exists()


# ---------------------------------------------------------------------------
# Scan interaction
# ---------------------------------------------------------------------------


def test_publish_hard_scan_failure_aborts(
    scaffolded_publishable, axiom_home: Path, capsys
) -> None:
    ext = scaffolded_publishable("hardscan_ext")
    # Swap the license to something off-allowlist -> hard fail.
    manifest = ext / "axiom-extension.toml"
    manifest.write_text(
        manifest.read_text().replace(
            'license = "Apache-2.0"', 'license = "Proprietary-1.0"'
        ),
        encoding="utf-8",
    )
    rc, out = _run_publish_cli(ext, "--no-tag-check", capsys=capsys)
    assert rc == 1
    assert "scan" in out.lower() or "license" in out.lower()
    assert "hardscan_ext" not in list_extensions()


def test_publish_strict_scan_bumps_warnings_to_fail(
    scaffolded_publishable, axiom_home: Path, capsys
) -> None:
    ext = scaffolded_publishable("strict_ext")
    # Plant a secret pattern -> warning. Without --strict-scan, publish
    # should succeed (warning-only); with --strict-scan it must abort.
    (ext / "strict_ext" / "commands" / "placeholder.py").write_text(
        "AWS_SECRET_ACCESS_KEY = 'wJalrXUtnFEMI/K7MDENGbPxRfiCYEXAMPLE2'\n",
        encoding="utf-8",
    )
    rc, out = _run_publish_cli(
        ext, "--no-tag-check", "--strict-scan", capsys=capsys
    )
    assert rc == 1
    assert "scan" in out.lower() or "warn" in out.lower()


# ---------------------------------------------------------------------------
# Validate interaction
# ---------------------------------------------------------------------------


def test_publish_validate_failure_aborts(
    scaffolded_publishable, axiom_home: Path, capsys
) -> None:
    ext = scaffolded_publishable("val_ext")
    # Break the validate step: declare a public symbol that doesn't exist.
    init_path = ext / "val_ext" / "__init__.py"
    init_path.write_text(
        '"""val_ext"""\n__all__ = ["does_not_exist"]\n',
        encoding="utf-8",
    )
    rc, out = _run_publish_cli(ext, "--no-tag-check", capsys=capsys)
    assert rc == 1


# ---------------------------------------------------------------------------
# Tag check (git)
# ---------------------------------------------------------------------------


from axiom.extensions.builtins.hygiene._git_isolation import (
    assert_test_tmp_path as _assert_test_tmp_path,
    git_isolated_env as _git_isolated_env,
)


def _init_git_repo(path: Path, *, tag: str | None = None) -> None:
    _assert_test_tmp_path(path)
    env = _git_isolated_env()
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "t@t.test"],
        check=True, env=env,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "tester"],
        check=True, env=env,
    )
    # Avoid GPG signing + default annotated-tag policies from the ambient
    # ~/.gitconfig that could break `git tag` in CI sandboxes.
    subprocess.run(
        ["git", "-C", str(path), "config", "commit.gpgsign", "false"],
        check=True, env=env,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "tag.gpgsign", "false"],
        check=True, env=env,
    )
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "init"],
        check=True, env=env,
    )
    if tag:
        # Use -m to force an annotated tag; some environments reject
        # lightweight tags by default.
        subprocess.run(
            ["git", "-C", str(path), "tag", "-a", tag, "-m", f"release {tag}"],
            check=True, env=env,
        )


def test_publish_in_git_repo_without_tag_warns_by_default(
    scaffolded_publishable, axiom_home: Path, capsys
) -> None:
    """Unit 10: default is warn-only, not fail-hard."""
    ext = scaffolded_publishable("tagged_ext")
    _init_git_repo(ext)  # No matching tag.
    rc, out = _run_publish_cli(ext, capsys=capsys)
    assert rc == 0, out
    assert "warning" in out.lower() and "tag" in out.lower()


def test_publish_in_git_repo_strict_tag_check_fails(
    scaffolded_publishable, axiom_home: Path, capsys
) -> None:
    """Unit 10: --strict-tag-check restores the old fail-hard behaviour."""
    ext = scaffolded_publishable("strict_tag_ext")
    _init_git_repo(ext)  # No matching tag.
    rc, out = _run_publish_cli(ext, "--strict-tag-check", capsys=capsys)
    assert rc == 1
    assert "tag" in out.lower()


def test_publish_in_git_repo_with_matching_tag_passes(
    scaffolded_publishable, axiom_home: Path, capsys
) -> None:
    ext = scaffolded_publishable("v_ext")
    _init_git_repo(ext, tag="v0.1.0")
    rc, out = _run_publish_cli(ext, capsys=capsys)
    assert rc == 0, out


def test_publish_no_tag_check_honored_with_deprecation_note(
    scaffolded_publishable, axiom_home: Path, capsys
) -> None:
    ext = scaffolded_publishable("skip_tag_ext")
    _init_git_repo(ext)  # No matching tag.
    rc, out = _run_publish_cli(ext, "--no-tag-check", capsys=capsys)
    assert rc == 0, out
    assert "deprecated" in out.lower()


def test_publish_outside_git_skips_tag_check(
    scaffolded_publishable, axiom_home: Path, capsys
) -> None:
    # Default scaffolded_publishable is NOT in a git repo; the tag check
    # should just silently pass.
    ext = scaffolded_publishable("nogit_ext")
    rc, out = _run_publish_cli(ext, capsys=capsys)
    assert rc == 0, out


# ---------------------------------------------------------------------------
# Registry URL override
# ---------------------------------------------------------------------------


def test_publish_registry_flag_honors_file_url(
    scaffolded_publishable, axiom_home: Path, tmp_path: Path, capsys
) -> None:
    alt_registry = tmp_path / "alt-registry"
    alt_registry.mkdir()
    ext = scaffolded_publishable("alt_ext")
    rc, out = _run_publish_cli(
        ext,
        "--no-tag-check",
        "--registry",
        f"file://{alt_registry}",
        capsys=capsys,
    )
    assert rc == 0, out
    # Artifact landed in the override, not the default.
    assert (alt_registry / "alt_ext" / "0.1.0" / "alt_ext-0.1.0.tar.gz").exists()


def test_publish_registry_rejects_non_file_url(
    scaffolded_publishable, axiom_home: Path, capsys
) -> None:
    ext = scaffolded_publishable("https_ext")
    rc, out = _run_publish_cli(
        ext,
        "--no-tag-check",
        "--registry",
        "https://example.com/registry",
        capsys=capsys,
    )
    assert rc == 1
    assert "file://" in out or "scheme" in out.lower()
