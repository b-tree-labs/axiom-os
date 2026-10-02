# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Prerelease local-wheel verification.

The existing ``test_clean_install.py`` installs the pinned version from
PyPI — which by construction can only check what's already published.
That leaves a gap: a bad wheel can be built, tagged, and pushed to PyPI
before any test inspects it. That's exactly how v0.10.10 shipped without
the classroom extension — the classroom files weren't in the git tree
at tag time, hatchling's default VCS-aware file inclusion excluded the
directory, and no pre-publish gate caught it.

This suite closes that gap. It:

1. Builds the wheel from the current source tree (``python -m build``)
2. Installs that local wheel in a clean Docker container
3. Exercises the same smoke tests as ``test_clean_install.py``
4. Additionally asserts that every builtin extension directory in the
   source tree appears in the installed wheel — catches the silent
   "extension missing from wheel" class of bug directly.

Opt in with ``-m install_path``. The recommended CI flow wires this
suite into a ``prerelease-wheel-check`` job that gates publish on
green here, so a broken wheel can never reach PyPI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BUILTINS_DIR = REPO_ROOT / "src" / "axiom" / "extensions" / "builtins"
BASE_IMAGE = "python:3.12-slim"
CONTAINER_NAME = "axi-prerelease-wheel-check"

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
    pyproject = REPO_ROOT / "pyproject.toml"
    import tomllib

    data = tomllib.loads(pyproject.read_text())
    return data["project"]["version"]


def _locate_or_build_wheel(tmp_path: Path) -> Path:
    """Find or build the wheel to exercise — must match the current version.

    Priority:
      1. ``AXI_WHEEL_PATH`` env var — CI jobs set this after downloading
         the build artifact for the in-flight release.
      2. ``<repo>/dist/axiom_os_lm-<current_version>-*.whl`` — version-
         matched to what pyproject.toml currently declares. Stale wheels
         from older versions are intentionally ignored; this test must
         exercise the *current* tree, not a historical one.
      3. Build fresh with ``python -m build --wheel`` into a tmp dir.
    """
    override = os.environ.get("AXI_WHEEL_PATH")
    if override:
        p = Path(override)
        if not p.is_file():
            pytest.skip(f"AXI_WHEEL_PATH points to missing file: {p}")
        return p

    current_version = _read_current_version()
    dist_dir = REPO_ROOT / "dist"
    if dist_dir.is_dir():
        version_matched = sorted(
            dist_dir.glob(f"axiom_os_lm-{current_version}-*.whl")
        )
        if version_matched:
            return version_matched[-1]

    # Build fresh — takes ~10–30s. Keeps the test hermetic: no dependency
    # on whether the developer has a stale `dist/` lying around.
    build_dir = tmp_path / "dist"
    build_dir.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(build_dir), str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if r.returncode != 0:
        pytest.fail(
            f"local wheel build failed:\nstdout: {r.stdout}\nstderr: {r.stderr}"
        )
    wheels = sorted(build_dir.glob(f"axiom_os_lm-{current_version}-*.whl"))
    if not wheels:
        # Fall back to any wheel — but warn in the failure message.
        wheels = sorted(build_dir.glob("axiom_os_lm-*.whl"))
        if not wheels:
            pytest.fail(
                f"`python -m build` succeeded but produced no wheel in {build_dir}"
            )
    return wheels[-1]


@pytest.fixture(scope="module")
def local_wheel(tmp_path_factory) -> Path:
    tmp = tmp_path_factory.mktemp("wheel-build")
    return _locate_or_build_wheel(tmp)


@pytest.fixture(scope="module")
def prerelease_container(request, local_wheel: Path):
    ok, reason = _docker_available()
    if not ok:
        pytest.skip(f"prerelease: {reason}")

    # Clean any stale container from a prior run.
    subprocess.run(
        ["docker", "rm", "-f", CONTAINER_NAME], capture_output=True, text=True, timeout=30
    )

    # Start container with the wheel mounted read-only at /wheel.
    start = subprocess.run(
        [
            "docker", "run", "-d", "--rm",
            "--name", CONTAINER_NAME,
            "-v", f"{local_wheel.parent}:/wheel:ro",
            BASE_IMAGE,
            "sleep", "600",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if start.returncode != 0:
        pytest.skip(
            f"prerelease: failed to start {BASE_IMAGE}: "
            f"{start.stderr.strip() or start.stdout.strip()}"
        )

    request.addfinalizer(
        lambda: subprocess.run(
            ["docker", "rm", "-f", CONTAINER_NAME],
            capture_output=True,
            text=True,
            timeout=30,
        )
    )

    wheel_in_container = f"/wheel/{local_wheel.name}"
    install = subprocess.run(
        [
            "docker", "exec", CONTAINER_NAME,
            "pip", "install", "--no-cache-dir", "--quiet",
            wheel_in_container,
        ],
        capture_output=True,
        text=True,
        timeout=240,
    )
    if install.returncode != 0:
        pytest.fail(
            f"pip install {wheel_in_container} failed:\n"
            f"stdout: {install.stdout}\nstderr: {install.stderr}"
        )

    return {"name": CONTAINER_NAME, "wheel": local_wheel.name}


def _exec(container: str, argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", container, *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _source_tree_extensions() -> list[str]:
    """Names of all builtin extension directories in the source tree.

    Every subdirectory of src/axiom/extensions/builtins/ that contains
    an extension manifest counts as a shippable extension — if any are
    missing from the built wheel, the wheel is broken.
    """
    names: list[str] = []
    for child in sorted(BUILTINS_DIR.iterdir()):
        if not child.is_dir() or child.name.startswith("__"):
            continue
        has_manifest = (
            (child / "axiom-extension.toml").exists()
            or (child / "neut-extension.toml").exists()
        )
        if has_manifest:
            names.append(child.name)
    return names


def test_local_wheel_ships_every_source_tree_extension(prerelease_container, local_wheel):
    """The single most important test in this suite.

    Enumerates every builtin extension directory in the source tree and
    asserts each appears in the installed package inside the container.
    Directly catches the v0.10.10 failure mode — a hatchling VCS
    exclusion silently dropping an extension from the wheel.
    """
    expected = _source_tree_extensions()
    assert expected, "no source-tree extensions found — sanity check failed"

    r = _exec(
        prerelease_container["name"],
        [
            "python", "-c",
            "import os, axiom; "
            "builtins = os.path.join(os.path.dirname(axiom.__file__), "
            "'extensions', 'builtins'); "
            "print('\\n'.join(sorted(os.listdir(builtins))))",
        ],
    )
    assert r.returncode == 0, (
        f"failed to list installed builtins: {r.stdout}{r.stderr}"
    )
    installed = {line.strip() for line in r.stdout.splitlines() if line.strip()}

    missing = [ext for ext in expected if ext not in installed]
    assert not missing, (
        f"Built wheel is missing extensions present in the source tree: "
        f"{missing}. Installed: {sorted(installed)}. This is the v0.10.10 "
        f"failure mode — likely cause: files exist on disk but are not "
        f"tracked in git, so hatchling's VCS-aware inclusion dropped them."
    )


def test_local_wheel_classroom_help_works(prerelease_container):
    """`axi classroom --help` must resolve on the fresh wheel.

    Direct Prague-critical path — classroom is the central extension
    for the summer 2026 cohort. A wheel that passes the extension-
    listing test above but fails here has a different bug
    (dispatcher / entry-point wiring).
    """
    r = _exec(
        prerelease_container["name"],
        ["sh", "-c", "HOME=/tmp/axi-home axi classroom --help"],
    )
    combined = r.stdout + r.stderr
    assert "invalid choice" not in combined.lower(), (
        f"classroom extension not discovered on local wheel: {combined}"
    )
    assert "unknown subcommand" not in combined.lower(), (
        f"classroom extension not routed on local wheel: {combined}"
    )
    assert "ImportError" not in combined, (
        f"classroom extension import failed: {combined}"
    )
    assert r.returncode == 0, f"axi classroom --help failed: {combined}"


def test_local_wheel_core_commands_work(prerelease_container):
    """Non-classroom core commands also need to work on the fresh wheel.

    Uses `--all` because tier-aware filtering hides `federation` /
    `nodes` from starter-tier users by default (their manifests sit at
    the schema-default `core` tier, and a fresh container has no
    competency.json so it operates as a starter user). This test
    verifies the CLI dispatcher resolves these nouns, not that they
    surface at first-run.
    """
    r = _exec(
        prerelease_container["name"],
        ["sh", "-c", "HOME=/tmp/axi-home axi --help --all"],
    )
    assert r.returncode == 0, f"axi --help failed: {r.stdout}{r.stderr}"
    out = r.stdout + r.stderr
    for token in ("federation", "nodes"):
        assert token in out, f"expected {token!r} in axi --help output"


def test_local_wheel_auth_stack_works_on_base_deps(prerelease_container):
    """`axiom.webauth` + the `gate` CLI must work on a base install.

    webauth imports PyJWT at module scope, but PyJWT used to be present
    only transitively (boxsdk[jwt] via the [extraction] extra; the
    herald-teams extra) — so every unit-test lane had it while a plain
    `pip install axiom-os-lm` failed on `import axiom.webauth`. A base
    install is the only environment that exercises the true dependency
    closure; this pins the auth stack to it at the publish gate.
    """
    r = _exec(
        prerelease_container["name"],
        [
            "python", "-c",
            "import axiom.webauth, axiom.extensions.builtins.webgate.api.routers",
        ],
    )
    combined = r.stdout + r.stderr
    assert r.returncode == 0, (
        f"auth stack import failed on base install (missing base dep?): {combined}"
    )

    # End-to-end through the CLI dispatcher: `gate list` on an uncreated
    # accounts file is defined to succeed with zero items.
    r = _exec(
        prerelease_container["name"],
        [
            "sh", "-c",
            "HOME=/tmp/axi-home python -m axiom.extensions.builtins.webgate "
            "--accounts-file /tmp/axi-gate-users.json --json list",
        ],
    )
    combined = r.stdout + r.stderr
    assert r.returncode == 0, f"gate CLI failed on base install: {combined}"
    assert '"items": []' in r.stdout, (
        f"gate list --json did not return the empty-accounts shape: {combined}"
    )


def test_local_wheel_federation_init_works(prerelease_container):
    r = _exec(
        prerelease_container["name"],
        ["sh", "-c", "HOME=/tmp/axi-home mkdir -p /tmp/axi-home && HOME=/tmp/axi-home axi federation init"],
        timeout=60,
    )
    combined = r.stdout + r.stderr
    assert "ImportError" not in combined, (
        f"federation init hit ImportError on local wheel: {combined}"
    )
    assert r.returncode == 0, f"axi federation init failed: {combined}"
