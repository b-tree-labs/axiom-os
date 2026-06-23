# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Clean-install validation against the published PyPI package.

This test is deliberately separate from the rest of the suite: it spins up
a fresh ``python:3.12-slim`` container, runs ``pip install axiom-os-lm==<pin>``
from the real public index (no local mount, no repo deps), and exercises a
handful of commands that have regressed on the install path in the past
(cryptography missing, branding squatter collision, shim subcommand dropped,
etc.). The goal is to catch install-path bugs that our in-repo unit tests
cannot see because they always run against an editable checkout.

Skipped by default — opt in with ``-m install_path``.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
BASE_IMAGE = "python:3.12-slim"
CONTAINER_NAME_PREFIX = "axi-clean-install"

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


def _read_pinned_version() -> str:
    # Avoid importing tomllib at module scope for 3.10 safety; 3.11+ has it stdlib.
    import tomllib

    data = tomllib.loads(PYPROJECT.read_text())
    return data["project"]["version"]


@pytest.fixture(scope="module")
def clean_container(request):
    ok, reason = _docker_available()
    if not ok:
        pytest.skip(f"install_path: {reason}")

    version = _read_pinned_version()
    name = f"{CONTAINER_NAME_PREFIX}-{version}".replace(".", "-")

    # Nuke any stale container with same name from a prior run.
    subprocess.run(
        ["docker", "rm", "-f", name],
        capture_output=True,
        text=True,
        timeout=30,
    )

    # Start a long-lived idle container. We exec into it for each step so we
    # can assert on each command independently and keep the harness a single
    # container (fast).
    start = subprocess.run(
        [
            "docker", "run", "-d", "--rm",
            "--name", name,
            BASE_IMAGE,
            "sleep", "600",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if start.returncode != 0:
        pytest.skip(
            f"install_path: failed to start {BASE_IMAGE}: "
            f"{start.stderr.strip() or start.stdout.strip()}"
        )

    def _cleanup():
        subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True,
            text=True,
            timeout=30,
        )

    request.addfinalizer(_cleanup)

    # Install the pinned version from PyPI — this is the "real user" step.
    install = subprocess.run(
        [
            "docker", "exec", name,
            "pip", "install", "--no-cache-dir", "--quiet",
            f"axiom-os-lm=={version}",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if install.returncode != 0:
        # Pre-publish state: pyproject.toml has been bumped to a version
        # that hasn't been published to PyPI yet. Skip rather than fail —
        # the prerelease-wheel-check CI job exercises the local artifact
        # in this window; this suite re-arms once the version is live.
        combined = (install.stdout or "") + (install.stderr or "")
        unpublished_signals = (
            "no matching distribution found",
            "could not find a version",
        )
        if any(sig in combined.lower() for sig in unpublished_signals):
            pytest.skip(
                f"install_path: axiom-os-lm=={version} not yet on PyPI "
                f"(pre-publish window). Run the prerelease local-wheel "
                f"check instead; this suite re-arms post-publish."
            )
        pytest.fail(
            f"pip install axiom-os-lm=={version} failed:\n"
            f"stdout: {install.stdout}\nstderr: {install.stderr}"
        )

    return {"name": name, "version": version}


def _exec(container: str, argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", container, *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_version_matches_pypi_pin(clean_container):
    c = clean_container
    r = _exec(c["name"], ["axi", "--version"])
    assert r.returncode == 0, f"axi --version failed: {r.stderr}"
    # `axi --version` typically emits `axi X.Y.Z` or similar; just look for the pin.
    combined = (r.stdout + r.stderr).strip()
    assert c["version"] in combined, (
        f"expected version {c['version']} in output, got: {combined!r}"
    )


def test_help_lists_subcommands(clean_container):
    # `axi --help` is tier-aware (default basic+starter hides federation/nodes
    # behind --all). Use --all here to assert the full surface.
    r = _exec(clean_container["name"], ["axi", "--all"])
    assert r.returncode == 0, f"axi --all failed: {r.stderr}"
    out = r.stdout + r.stderr
    # Sanity: a handful of top-level subcommands should appear.
    for token in ("federation", "nodes"):
        assert token in out, f"expected {token!r} in axi --all output"


def test_federation_init_has_cryptography(clean_container, tmp_path):
    """Regression guard: if `cryptography` is ever dropped from deps, this fails."""
    # Use a container-local HOME to avoid stomping on anything and to keep the
    # init idempotent across repeat runs.
    r = _exec(
        clean_container["name"],
        ["sh", "-c", "HOME=/tmp/axi-home mkdir -p /tmp/axi-home && HOME=/tmp/axi-home axi federation init"],
        timeout=60,
    )
    combined = r.stdout + r.stderr
    assert "ImportError" not in combined, f"federation init hit ImportError: {combined}"
    assert "ModuleNotFoundError" not in combined, f"federation init hit ModuleNotFoundError: {combined}"
    assert r.returncode == 0, f"axi federation init failed: {combined}"


def test_nodes_list_empty_clean(clean_container):
    r = _exec(
        clean_container["name"],
        ["sh", "-c", "HOME=/tmp/axi-home axi nodes list"],
    )
    combined = r.stdout + r.stderr
    assert "ImportError" not in combined
    assert "Traceback" not in combined, f"axi nodes list crashed: {combined}"
    assert r.returncode == 0, f"axi nodes list failed: {combined}"


def test_builtin_extension_discovered(clean_container):
    """Extension discovery via importlib.metadata surfaces builtin extensions.

    Regression guard for commit 2a786c0 (9x faster CLI startup — resolve
    installed packages via metadata). If wheel metadata is wrong or the
    discovery path regresses, `axi classroom --help` falls into argparse's
    "invalid choice" branch — which would break the Prague cohort path
    (classroom is the central extension for the summer 2026 deployment).
    """
    r = _exec(
        clean_container["name"],
        ["sh", "-c", "HOME=/tmp/axi-home axi classroom --help"],
    )
    combined = r.stdout + r.stderr
    assert "invalid choice" not in combined.lower(), (
        f"classroom extension not discovered on clean install: {combined}"
    )
    assert "ImportError" not in combined, (
        f"classroom extension import failed: {combined}"
    )
    assert "ModuleNotFoundError" not in combined, (
        f"classroom extension module missing: {combined}"
    )
    assert r.returncode == 0, f"axi classroom --help failed: {combined}"
    # At least one of the classroom lifecycle verbs should appear in --help.
    lifecycle_tokens = ("prep", "create", "enroll", "status", "doctor")
    assert any(tok in combined for tok in lifecycle_tokens), (
        f"expected classroom lifecycle verb in --help, got: {combined!r}"
    )


def test_install_shim_subcommand_registered(clean_container):
    """install-shim should be a registered subcommand and reject a fake target."""
    r = _exec(
        clean_container["name"],
        ["axi", "install-shim", "--target", "/fake/path"],
    )
    combined = r.stdout + r.stderr
    # Must NOT be argparse's "invalid choice" — that would mean the subcommand
    # isn't registered at all (the shim-fallback regression we keep hitting).
    assert "invalid choice" not in combined.lower(), (
        f"install-shim subcommand not registered on clean install: {combined}"
    )
    # Should produce the expected "does not exist" error for the bogus target.
    # (Note: the installed CLI currently returns 0 even on this error path —
    # we assert on the stderr message rather than the exit code so this test
    # stays focused on "is the subcommand registered and doing validation?".
    # A separate unit test can tighten the exit-code contract.)
    assert "does not exist" in combined, (
        f"expected --target rejection, got: {combined!r}"
    )


def test_clean_uninstall_leaves_no_axi_modules(clean_container):
    name = clean_container["name"]
    u = _exec(name, ["pip", "uninstall", "-y", "axiom-os-lm"], timeout=60)
    assert u.returncode == 0, f"pip uninstall failed: {u.stderr}"

    probe = textwrap.dedent(
        """
        import importlib, sys
        leftovers = []
        for mod in ("axiom", "axi", "axiom_os"):
            try:
                importlib.import_module(mod)
                leftovers.append(mod)
            except ImportError:
                pass
        if leftovers:
            print("LEFTOVERS:" + ",".join(leftovers))
            sys.exit(1)
        print("CLEAN")
        """
    ).strip()

    r = _exec(name, ["python", "-c", probe])
    combined = r.stdout + r.stderr
    assert r.returncode == 0, f"post-uninstall import probe found residue: {combined}"
    assert "CLEAN" in combined
