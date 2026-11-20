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
    deployments = {"traefik": 1, "cloudnative-pg": 1, "seaweedfs": 1}
    objects[("secret", "object-store")] = {}
    for key, value in overrides.items():
        if key in deployments:
            deployments[key] = value
        else:
            kind, name = key.split("__")
            objects[(kind, name.replace("_", "-"))] = value

    def k(args):
        if args[:2] == ["get", "deployments"]:
            app = args[-1].split("=", 1)[1].split(",", 1)[0]
            if app == "seaweedfs" and deployments[app] is None:
                return {"items": []}
            return {"items": [{"status": {"availableReplicas": deployments.get(app, 0)}}]}
        return objects.get((args[1], args[2]))

    return k


def test_a_cluster_with_every_name_passes():
    checks = run(cluster())
    assert [c.ok for c in checks] == [True] * 7, checks


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
        ({"seaweedfs": 0}, "object store"),
        ({"secret__object_store": None}, "object store"),
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


def test_an_object_store_the_platform_provides_is_not_ours_to_check():
    checks = run(cluster(seaweedfs=None))
    obj = next(c for c in checks if c.name == "object store")
    assert obj.ok and "provided by the platform" in obj.detail


def test_the_object_store_keys_survive_an_upgrade_and_reach_the_store():
    docs = _render("values-small.yaml")
    keys = next(
        d
        for d in docs
        if d["kind"] == "Secret" and d["metadata"]["name"] == "axiom-object-store-keys"
    )
    assert set(keys["stringData"]) == {
        "admin_access_key_id",
        "admin_secret_access_key",
        "read_access_key_id",
        "read_secret_access_key",
    }
    assert keys["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    entry = next(
        d for d in docs if d["kind"] == "Secret" and d["metadata"]["name"] == "object-store"
    )
    assert entry["metadata"]["namespace"] == "axiom-secrets"
    assert entry["stringData"]["AWS_ACCESS_KEY_ID"] == keys["stringData"]["admin_access_key_id"]
    assert entry["stringData"]["AXIOM_S3_ENDPOINT"].endswith(":8333")
    # The gateway's config names the keys through the environment, never inline.
    gw = next(
        d
        for d in docs
        if d["kind"] == "Secret" and d["metadata"]["name"].endswith("seaweedfs-s3-secret")
    )
    assert "${SEAWEEDFS_S3_ADMIN_ACCESS_KEY_ID}" in str(gw)


def test_no_object_store_server_keeps_its_data_on_a_host_path():
    for files in (("values-small.yaml",), ("values-large.yaml",)):
        for d in _render(*files):
            spec = d.get("spec", {}) if isinstance(d.get("spec"), dict) else {}
            pod = (
                spec.get("template", {}).get("spec", {})
                if d["kind"] in ("Deployment", "StatefulSet")
                else {}
            )
            assert not [v for v in pod.get("volumes") or [] if "hostPath" in v], (
                files,
                d["metadata"]["name"],
            )


def test_a_cloud_with_its_own_s3_gets_no_seaweedfs():
    for provider in ("values-provider-aws.yaml", "values-provider-gcp.yaml"):
        names = {d["metadata"]["name"] for d in _render("values-large.yaml", provider)}
        assert not [n for n in names if "seaweedfs" in n], provider


def test_a_gpu_cluster_gets_the_device_plugin_and_the_runtime_class_the_node_chart_names():
    without = {(d["kind"], d["metadata"]["name"]) for d in _render("values-medium.yaml")}
    assert ("RuntimeClass", "nvidia") not in without
    docs = subprocess.run(
        [
            "helm",
            "template",
            "c",
            str(CHART),
            "-n",
            "axiom-system",
            "-f",
            str(CHART / "values-medium.yaml"),
            "--set",
            "gpu.install=true",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    names = {(d["kind"], d["metadata"]["name"]) for d in yaml.safe_load_all(docs) if d}
    assert ("RuntimeClass", "nvidia") in names
    assert any(k == "DaemonSet" and "nvidia-device-plugin" in n for k, n in names)


def test_monitoring_runs_on_the_kubernetes_sizes_above_small_and_keeps_its_data_on_a_claim():
    assert not [d for d in _render("values-small.yaml") if d["kind"] == "Prometheus"]
    for files in (
        ("values-medium.yaml",),
        ("values-large.yaml",),
        ("values-large.yaml", "values-provider-aws.yaml"),
    ):
        docs = _render(*files)
        prom = [d for d in docs if d["kind"] == "Prometheus"]
        assert prom, files
        vct = prom[0]["spec"]["storage"]["volumeClaimTemplate"]["spec"]
        assert vct["storageClassName"] == "axiom-standard"
        assert any(
            d["kind"] == "DaemonSet" and "node-exporter" in d["metadata"]["name"] for d in docs
        )
