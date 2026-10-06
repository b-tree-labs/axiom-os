# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Upgrade-path validation — install an older release, then upgrade to current.

Editable-install unit tests can't see regressions that only appear when a
user upgrades from a prior PyPI release. This harness exercises that path:
install `axiom-os-lm==<prior_version>` in a clean container, upgrade to the
version pinned in pyproject.toml, and verify core commands + extension
discovery still work after the upgrade.

Why this catches things unit tests miss:
- Schema migrations between versions
- Entry-point metadata changes that only manifest on fresh wheels
- Removed/renamed modules that older state references
- Config-file format drift without migration

Skipped by default — opt in with ``-m install_path``. Required CI gate
(see .github/workflows/ci.yml).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
BASE_IMAGE = "python:3.12-slim"
CONTAINER_NAME_PREFIX = "axi-upgrade-graph"

# Historical versions to test upgrades from. Pick hops that exercise distinct
# installer phases; grow this list as we ship new versions. A missing version
# on PyPI skips the hop rather than failing — the catalog grows, old hops
# stay pinned as history.
#
# v0.10.6 — branding package_name drift fix (see install/upgrade hardening memory).
# v0.10.10 — extension-metadata discovery optimization (commit 2a786c0).
PRIOR_VERSIONS = ["0.10.6"]

pytestmark = pytest.mark.install_path


def _docker_available() -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "docker CLI not on PATH"
    try:
        r = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"docker not responsive: {exc}"
    if r.returncode != 0:
        return False, f"docker daemon unreachable: {r.stderr.strip() or r.stdout.strip()}"
    return True, ""


def _read_current_version() -> str:
    import tomllib

    data = tomllib.loads(PYPROJECT.read_text())
    return data["project"]["version"]


def _exec(container: str, argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", container, *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _start_container(name: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", name], capture_output=True, text=True, timeout=30
    )
    start = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, BASE_IMAGE, "sleep", "900"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if start.returncode != 0:
        pytest.skip(
            f"upgrade_graph: failed to start {BASE_IMAGE}: "
            f"{start.stderr.strip() or start.stdout.strip()}"
        )


def _pip_install_version(container: str, version: str, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "docker", "exec", container,
            "pip", "install", "--no-cache-dir", "--quiet",
            f"axiom-os-lm=={version}",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def current_version() -> str:
    return _read_current_version()


@pytest.mark.parametrize("prior_version", PRIOR_VERSIONS)
def test_upgrade_from_prior_preserves_core_commands(prior_version: str, current_version: str, request):
    """Install prior version, run core commands, upgrade, run the same commands.

    Failure modes caught:
    - Upgrade breaks a command that used to work (regression)
    - Upgrade leaves state incompatible with the new schema
    - Extension discovery on the upgraded wheel loses an extension that was
      present in the prior version (the Prague-critical regression vector)
    """
    ok, reason = _docker_available()
    if not ok:
        pytest.skip(f"upgrade_graph: {reason}")

    name = f"{CONTAINER_NAME_PREFIX}-{prior_version}-to-{current_version}".replace(".", "-")
    _start_container(name)
    request.addfinalizer(
        lambda: subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=30)
    )

    # === Phase 1: install prior version ===
    r_prior = _pip_install_version(name, prior_version)
    if r_prior.returncode != 0:
        # Prior version not on PyPI (yanked, or never published). Skip — the
        # test's purpose is to validate the upgrade hop, not gate on
        # historical availability.
        pytest.skip(
            f"upgrade_graph: axiom-os-lm=={prior_version} unavailable: "
            f"{r_prior.stderr.strip() or r_prior.stdout.strip()}"
        )

    # Ensure the prior version at least installed + can print help. Don't
    # assert deeper behavior: some historical releases had known gaps
    # (e.g. 0.10.6 required cryptography without declaring it core). The
    # point of this test is to prove the UPGRADE leaves us in a good
    # state, not to retroactively gate on prior-version quality.
    r_help_prior = _exec(name, ["sh", "-c", "HOME=/tmp/axi-home axi --help"])
    assert r_help_prior.returncode == 0, (
        f"prior version {prior_version} couldn't even print help "
        f"(pre-upgrade baseline too broken to meaningfully test upgrade): "
        f"{r_help_prior.stdout}{r_help_prior.stderr}"
    )

    # Make /tmp/axi-home exist for post-upgrade checks. We intentionally
    # do NOT require federation init to work on the prior version — that
    # gated on extras that weren't stable until 0.10.6+.
    _exec(name, ["sh", "-c", "mkdir -p /tmp/axi-home"])

    # === Phase 2: upgrade to current ===
    r_upgrade = _pip_install_version(name, current_version)
    if r_upgrade.returncode != 0:
        # Pre-publish window — current_version is pinned in pyproject.toml
        # but hasn't landed on PyPI yet. Skip rather than fail; the
        # prerelease-wheel-check CI job exercises the local artifact in
        # this window. This suite re-arms once current_version is live.
        combined = (r_upgrade.stdout or "") + (r_upgrade.stderr or "")
        unpublished_signals = (
            "no matching distribution found",
            "could not find a version",
        )
        if any(sig in combined.lower() for sig in unpublished_signals):
            pytest.skip(
                f"upgrade_graph: axiom-os-lm=={current_version} not yet "
                f"on PyPI (pre-publish window). Run the prerelease local-"
                f"wheel check instead."
            )
        pytest.fail(
            f"upgrade {prior_version} → {current_version} failed: "
            f"stdout={r_upgrade.stdout} stderr={r_upgrade.stderr}"
        )

    # === Phase 3: core commands still work post-upgrade ===
    r_help_post = _exec(name, ["sh", "-c", "HOME=/tmp/axi-home axi --help"])
    combined_help = r_help_post.stdout + r_help_post.stderr
    assert "ImportError" not in combined_help, (
        f"post-upgrade `axi --help` hit ImportError: {combined_help}"
    )
    assert "ModuleNotFoundError" not in combined_help, (
        f"post-upgrade `axi --help` hit ModuleNotFoundError: {combined_help}"
    )
    assert r_help_post.returncode == 0, f"post-upgrade `axi --help` failed: {combined_help}"

    # Post-upgrade federation init MUST work — this is the baseline
    # for federation-join ceremony (the Prague-critical path). If this
    # regresses, the upgrade has broken something load-bearing.
    r_init_post = _exec(
        name,
        ["sh", "-c", "HOME=/tmp/axi-home axi federation init"],
        timeout=60,
    )
    combined_init = r_init_post.stdout + r_init_post.stderr
    assert "Traceback" not in combined_init, (
        f"post-upgrade federation init crashed: {combined_init}"
    )
    assert "ImportError" not in combined_init and "No module named" not in combined_init, (
        f"post-upgrade federation init missing a dependency: {combined_init}"
    )
    assert r_init_post.returncode == 0, (
        f"post-upgrade federation init failed: {combined_init}"
    )

    # Extension discovery — the Prague-critical path. If the classroom
    # extension fell off after upgrade, the cohort can't enroll.
    r_classroom = _exec(
        name,
        ["sh", "-c", "HOME=/tmp/axi-home axi classroom --help"],
    )
    combined_classroom = r_classroom.stdout + r_classroom.stderr
    assert "invalid choice" not in combined_classroom.lower(), (
        f"post-upgrade classroom extension not discovered (Prague blocker): "
        f"{combined_classroom}"
    )
    assert r_classroom.returncode == 0, (
        f"post-upgrade `axi classroom --help` failed: {combined_classroom}"
    )
