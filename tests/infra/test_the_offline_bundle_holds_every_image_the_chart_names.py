# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The offline bundle holds every image the charts name, at each size it serves.

``infra/bundle/offline-bundle.toml`` is the bundle's list. A machine with no
network cannot pull what the list forgot, and it finds out as a pod that never
starts. So both charts are rendered here at small and medium, every image
reference is read out of the rendered text, and one that the list lacks fails
the test: a chart change that adds or moves an image has to change the list in
the same pull request.

The cluster base needs its chart dependencies downloaded (``helm dependency
build``); the cluster-base job in node-chart.yml does that and runs this file.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from axiom.infra.deploy import offline_bundle as ob

ROOT = Path(__file__).resolve().parents[2]
NODE = ROOT / "infra" / "charts" / "axiom-node"
CLUSTER = ROOT / "infra" / "charts" / "axiom-cluster"
DECLARATION = ROOT / "infra" / "bundle" / "offline-bundle.toml"
NODE_CHART_WORKFLOW = ROOT / ".github" / "workflows" / "node-chart.yml"

#: Every role and switch that brings an image of its own.
CORE_ROLES = (
    "roles.edge.enabled=true",
    "roles.producer.enabled=true",
    "roles.medallion.enabled=true",
    "roles.medallion.secretFrom=pg",
    "roles.forwarder.enabled=true",
    "roles.edgePull.enabled=true",
    "roles.edgePull.connector=c",
    "roles.twin.enabled=true",
    "roles.serve.enabled=true",
    "roles.rag.enabled=true",
    "roles.partnerHealth.enabled=true",
    "packages.wheelhouse.enabled=true",
    # As the installer sets it: the image the bundle built for the chart's pin.
    "packages.wheelhouse.image=axiom-wheelhouse:"
    + str(yaml.safe_load((NODE / "values.yaml").read_text())["packages"]["version"]),
)
CHAT_ROLES = ("roles.model.enabled=true", "roles.chat.enabled=true")

SIZES = ("small", "medium")


def render(chart: Path, size: str, *sets: str) -> str:
    if not shutil.which("helm"):
        pytest.skip("helm is not installed")
    args = ["helm", "template", "t", str(chart), "-f", str(chart / f"values-{size}.yaml")]
    for s in sets:
        args += ["--set", s]
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout


def declared(size: str, groups=None) -> set[str]:
    decl = ob.load_declaration(DECLARATION)
    return {ob.normalize_ref(i["ref"]) for i in ob.images_for(decl, [size], groups)}


def test_the_bundle_serves_small_and_medium():
    assert tuple(ob.load_declaration(DECLARATION)["sizes"]) == SIZES


@pytest.mark.parametrize("size", SIZES)
def test_every_image_the_node_chart_names_is_in_the_bundle(size):
    rendered = render(NODE, size, *CORE_ROLES, *CHAT_ROLES)
    named = ob.image_refs_in(rendered)
    assert len(named) >= 6, named  # runtime, front, database, wheelhouse, chat UI, model server
    # The offline package source is part of the chart, not a step done by hand.
    assert any(n.startswith("axiom-wheelhouse:") for n in named), named
    missing = named - declared(size, groups="all")
    assert not missing, f"{size}: the node chart names images the bundle list lacks: {sorted(missing)}"


@pytest.mark.parametrize("size", SIZES)
def test_the_default_bundle_covers_every_role_but_the_optional_group(size):
    """What a release attaches installs every role except the ones a group
    that is off by default serves; the installer refuses those by name."""
    named = ob.image_refs_in(render(NODE, size, *CORE_ROLES))
    missing = named - declared(size)
    assert not missing, f"{size}: not in the default groups: {sorted(missing)}"
    chat = ob.image_refs_in(render(NODE, size, *CORE_ROLES, *CHAT_ROLES)) - named
    assert chat and not (chat & declared(size)), "the chat group is off by default"


@pytest.mark.parametrize("size", SIZES)
def test_every_image_the_cluster_base_names_is_in_the_bundle(size):
    wanted = yaml.safe_load((CLUSTER / "Chart.yaml").read_text())["dependencies"]
    have = {p.name for p in (CLUSTER / "charts").glob("*.tgz")}
    if not all(f"{d['name']}-{d['version']}.tgz" in have for d in wanted):
        pytest.skip("run `helm dependency build infra/charts/axiom-cluster` first (CI's cluster-base job does)")
    decl = ob.load_declaration(DECLARATION)
    named = ob.image_refs_in(render(CLUSTER, size))
    assert named, "the cluster base rendered no image"
    known = declared(size) | {ob.normalize_ref(r["ref"]) for r in decl["referenced_not_bundled"]}
    missing = named - known
    assert not missing, f"{size}: the cluster base names images the bundle list lacks: {sorted(missing)}"


def test_an_image_the_list_lacks_is_caught():
    """The control: the check can fail."""
    rendered = """
    containers:
      - name: a
        image: "registry.example/new/thing:1.0"
      - image: nginx:1.27.3-alpine
    metadata:
      annotations:
        axiom.io/front-image: "docker.io/library/nginx:1.27.3-alpine"
    args:
      - --prometheus-config-reloader=quay.example/op/reloader:v1
      - --config-reloader-cpu-request=0
    imagePullPolicy: IfNotPresent
    """
    named = ob.image_refs_in(rendered)
    assert named == {"registry.example/new/thing:1.0", "nginx:1.27.3-alpine", "quay.example/op/reloader:v1"}
    assert "registry.example/new/thing:1.0" in named - declared("small", groups="all")


def test_every_listed_image_and_file_is_pinned_for_both_architectures():
    decl = ob.load_declaration(DECLARATION)
    for d in decl["download"]:
        for arch in ob.ARCHES:
            assert re.fullmatch(r"[0-9a-f]{64}", d["sha256"][arch]), (d["name"], arch)
            assert ob.download_url(d, arch).startswith("https://"), d["name"]
    for i in decl["image"]:
        assert ":" in i["ref"].rsplit("/", 1)[-1] and not i["ref"].endswith(":latest"), i["ref"]
        assert set(i["sizes"]) <= set(SIZES) and i["group"] in decl["groups"], i["ref"]


def test_the_pins_are_the_ones_the_charts_are_proven_with():
    """One version of each tool: the one node-chart.yml runs the proofs on."""
    decl = ob.load_declaration(DECLARATION)
    tools = {d["name"]: d["version"] for d in decl["download"]}
    workflow = NODE_CHART_WORKFLOW.read_text()
    assert set(re.findall(r"K3D_VERSION: (\S+)", workflow)) == {tools["k3d"]}
    assert f"version: {tools['helm']}" in workflow
    values = yaml.safe_load((NODE / "values.yaml").read_text())
    wheelhouse = next(i for i in decl["image"] if i.get("wheelhouse"))
    assert wheelhouse["ref"] == f"{decl['python']['wheelhouse_image']}:{values['packages']['version']}"
    assert decl["python"]["image"] == f"{values['image']['repository']}:{values['image']['tag']}"
    k3s = tools["k3s"]
    assert {tools["k3s-install"], tools["k3s-airgap-images"]} == {k3s}
    assert any(i["ref"] == "rancher/k3s:" + k3s.replace("+", "-") for i in decl["image"])
