# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A deployment has one size: micro, small, medium or large (ADR-215).

The names before it (lowpower, node, cluster) stay accepted for one minor
release, and an install that still uses one must get exactly what its size
gives, or the rename has changed a running node. A name that is not a size
fails the render: before, it produced roles with no resources at all.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

CHART = Path(__file__).resolve().parents[2] / "infra" / "charts" / "axiom-node"
SIZES = ("micro", "small", "medium", "large")
ALIASES = {"lowpower": "micro", "node": "small", "cluster": "large"}
EVERY_ROLE = (
    "roles.edge.enabled=true",
    "roles.producer.enabled=true",
    "roles.medallion.enabled=true",
    "roles.medallion.secretFrom=pg",
    "roles.forwarder.enabled=true",
    "roles.edgePull.enabled=true",
    "roles.edgePull.connector=c",
)


def helm(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    if not shutil.which("helm"):
        pytest.skip("helm is not installed")
    return subprocess.run(["helm", *args], capture_output=True, text=True, check=check)


def render(values_file: str, *extra: str) -> str:
    args = ["template", "t", str(CHART), "-f", str(CHART / values_file)]
    for s in (*EVERY_ROLE, *extra):
        args += ["--set", s]
    return helm(*args).stdout


@pytest.mark.parametrize("size", SIZES)
def test_every_size_has_a_values_file_that_names_it(size):
    assert f"profile: {size}\n" in (CHART / f"values-{size}.yaml").read_text()
    assert "resources:" in render(f"values-{size}.yaml")


@pytest.mark.parametrize("size", SIZES)
def test_every_kubernetes_size_uses_the_cluster_bases_names_and_micro_does_not(size):
    out = render(f"values-{size}.yaml")
    assert ("storageClassName: axiom-" in out) == (size != "micro")


@pytest.mark.parametrize("old,size", sorted(ALIASES.items()))
def test_an_old_name_renders_exactly_what_its_size_renders(old, size):
    assert render(f"values-{old}.yaml") == render(f"values-{size}.yaml")


def test_the_default_is_small_without_the_cluster_base():
    """A bare install sizes like small, and names no storage class, so it
    still runs on a cluster nobody prepared (the smoke clusters, a laptop)."""
    args = ["template", "t", str(CHART)]
    for s in EVERY_ROLE:
        args += ["--set", s]
    assert helm(*args).stdout == render("values-small.yaml", "contract.enabled=false")


def test_a_name_that_is_not_a_size_fails_the_render():
    out = helm("template", "t", str(CHART), "--set", "profile=bogus", check=False)
    assert out.returncode != 0
    assert 'profile "bogus" is not a size' in out.stderr


def test_an_install_on_an_old_name_is_told_its_size(tmp_path):
    # NOTES.txt renders only on an install, and Helm 3 needs a cluster for even a
    # client dry run. So render the real notes through a probe in a copy of the chart.
    chart = tmp_path / "chart"
    shutil.copytree(CHART, chart)
    shutil.copy(chart / "templates" / "NOTES.txt", chart / "probe-notes.txt")
    (chart / "templates" / "probe.yaml").write_text(
        'notes: {{ tpl (.Files.Get "probe-notes.txt") . | quote }}\n'
    )
    notes = helm(
        "template",
        "t",
        str(chart),
        "-f",
        str(chart / "values-lowpower.yaml"),
        "--show-only",
        "templates/probe.yaml",
    )
    assert 'deprecated name for the size "micro"' in notes.stdout.replace('\\"', '"')
