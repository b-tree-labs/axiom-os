# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A cluster meets the contract when every name the application chart uses is
there and ready (ADR-215), whoever provides it.

The check runs against a fake kubectl here; the cluster-base CI job runs it
against a real K3D cluster with the chart installed.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from axiom.infra.deploy.cluster_contract import run

CHART = Path(__file__).resolve().parents[2] / "infra" / "charts" / "axiom-cluster"
READY = {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}


def cluster(**overrides):
    objects = {
        ("storageclass", "axiom-fast"): {"provisioner": "rancher.io/local-path"},
        ("storageclass", "axiom-standard"): {"provisioner": "rancher.io/local-path"},
        ("ingressclass", "axiom"): {"spec": {"controller": "traefik.io/ingress-controller"}},
        ("clusterissuer", "axiom-selfsigned"): READY,
        ("crd", "clusters.postgresql.cnpg.io"): {},
        ("clustersecretstore", "axiom"): {**READY, "spec": {"provider": {"kubernetes": {}}}},
    }
    deployments = {"traefik": 1, "cloudnative-pg": 1}
    for key, value in overrides.items():
        if key in deployments:
            deployments[key] = value
        else:
            kind, name = key.split("__")
            objects[(kind, name.replace("_", "-"))] = value

    def k(args):
        if args[:2] == ["get", "deployments"]:
            app = args[-1].split("=", 1)[1]
            return {"items": [{"status": {"availableReplicas": deployments.get(app, 0)}}]}
        return objects.get((args[1], args[2]))

    return k


def test_a_cluster_with_every_name_passes():
    checks = run(cluster())
    assert [c.ok for c in checks] == [True] * 6, checks


@pytest.mark.parametrize(
    "override,failing",
    [
        ({"storageclass__axiom_fast": None}, "storage class axiom-fast"),
        ({"ingressclass__axiom": None}, "ingress class axiom"),
        ({"traefik": 0}, "ingress class axiom"),
        ({"clusterissuer__axiom_selfsigned": None}, "certificate issuer"),
        (
            {
                "clusterissuer__axiom_selfsigned": {
                    "status": {"conditions": [{"type": "Ready", "status": "False"}]}
                }
            },
            "certificate issuer",
        ),
        ({"crd__clusters.postgresql.cnpg.io": None}, "postgres operator"),
        ({"cloudnative-pg": 0}, "postgres operator"),
        ({"clustersecretstore__axiom": {"status": {}}}, "secret store axiom"),
    ],
)
def test_each_missing_or_unready_name_fails_its_own_check(override, failing):
    checks = run(cluster(**override))
    assert [c.name for c in checks if not c.ok] == [failing]


def test_a_class_served_by_another_controller_is_the_platforms_to_serve():
    checks = run(
        cluster(**{"ingressclass__axiom": {"spec": {"controller": "k8s.io/other"}}, "traefik": 0})
    )
    assert all(c.ok for c in checks)


def _render(*files: str) -> list[dict]:
    if not shutil.which("helm") or not (CHART / "charts").is_dir():
        pytest.skip("helm or the chart's dependencies (helm dependency build) are missing")
    args = ["helm", "template", "c", str(CHART)]
    for f in files:
        args += ["-f", str(CHART / f)]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


@pytest.mark.parametrize(
    "files",
    [
        ("values-small.yaml",),
        ("values-medium.yaml",),
        ("values-large.yaml",),
        ("values-large.yaml", "values-provider-aws.yaml"),
        ("values-large.yaml", "values-provider-gcp.yaml"),
        ("values-large.yaml", "values-provider-azure.yaml"),
    ],
)
def test_every_size_and_provider_renders_the_same_names(files):
    docs = _render(*files)
    names = {(d["kind"], d["metadata"]["name"]) for d in docs}
    assert {
        ("StorageClass", "axiom-fast"),
        ("StorageClass", "axiom-standard"),
        ("IngressClass", "axiom"),
        ("ClusterSecretStore", "axiom"),
    } <= names
    assert names & {("ClusterIssuer", "axiom-selfsigned"), ("ClusterIssuer", "axiom-letsencrypt")}
    # Only one controller owns the class: the chart never creates Traefik's own.
    assert ("IngressClass", "traefik") not in names


def test_a_platform_that_bundles_traefik_gets_no_second_one():
    kinds = {(d["kind"], d["metadata"]["name"]) for d in _render("values-small.yaml")}
    assert not any(k == "Deployment" and "traefik" in n for k, n in kinds)


def test_a_cloud_gets_traefik_installed_and_its_own_block_storage():
    docs = _render("values-large.yaml", "values-provider-aws.yaml")
    assert any(d["kind"] == "Deployment" and "traefik" in d["metadata"]["name"] for d in docs)
    fast = next(
        d for d in docs if d["kind"] == "StorageClass" and d["metadata"]["name"] == "axiom-fast"
    )
    assert fast["provisioner"] == "ebs.csi.aws.com"
