# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A node on Kubernetes uses the cluster base's names, never a provider's (ADR-215).

With `contract.enabled` (the small, medium and large sizes): every volume
claim names `axiom-fast` or `axiom-standard`, an Ingress uses the class `axiom`
and the issuer, and a Secret can come from the store `axiom`. Anything that
is not one of the fixed roles runs as a `service` on the same workload
definition, and a GPU is asked for in the same way everywhere. A single host
(Compose) ignores the cluster's objects and refuses what it cannot honour.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from axiom.infra.deploy import bluegreen
from axiom.infra.deploy.compose_from_chart import compose_from_manifests

CHART = Path(__file__).resolve().parents[2] / "infra" / "charts" / "axiom-node"


def helm(
    *sets: str, files: tuple[str, ...] = (), check: bool = True
) -> subprocess.CompletedProcess:
    if not shutil.which("helm"):
        pytest.skip("helm is not installed")
    args = ["helm", "template", "node", str(CHART)]
    for f in files:
        args += ["-f", str(CHART / f)]
    for s in sets:
        args += ["--set", s]
    return subprocess.run(args, capture_output=True, text=True, check=check)


def docs(*sets: str, files: tuple[str, ...] = ()) -> list[dict]:
    return [d for d in yaml.safe_load_all(helm(*sets, files=files).stdout) if d]


def by(kind: str, name: str, ds: list[dict]) -> dict:
    return next(d for d in ds if d["kind"] == kind and d["metadata"]["name"] == name)


def claims(ds: list[dict]) -> dict[str, str | None]:
    out = {}
    for d in ds:
        if d["kind"] == "PersistentVolumeClaim":
            out[d["metadata"]["name"]] = d["spec"].get("storageClassName")
        if d["kind"] == "StatefulSet":
            for vct in d["spec"].get("volumeClaimTemplates") or []:
                out[f"{d['metadata']['name']}/{vct['metadata']['name']}"] = vct["spec"].get(
                    "storageClassName"
                )
    return out


SMALL = ("values-small.yaml",)


def test_databases_and_model_caches_are_fast_and_everything_else_standard():
    ds = docs(
        "roles.medallion.enabled=true",
        "roles.medallion.secretFrom=pg",
        "roles.model.enabled=true",
        "roles.serve.enabled=true",
        "roles.producer.enabled=true",
        files=SMALL,
    )
    c = claims(ds)
    assert c["node-medallion/data"] == "axiom-fast"
    assert c["node-model-data"] == "axiom-fast"
    assert c["node-serve-data"] == "axiom-standard"
    assert c["node-producer/data"] == "axiom-standard"
    assert all(v in ("axiom-fast", "axiom-standard") for v in c.values()), c


def test_a_role_may_choose_its_storage_class():
    ds = docs("roles.serve.enabled=true", "roles.serve.storageClass=axiom-fast", files=SMALL)
    assert claims(ds)["node-serve-data"] == "axiom-fast"


def test_without_the_cluster_base_no_class_is_named():
    ds = docs(
        "roles.medallion.enabled=true", "roles.medallion.secretFrom=pg", "roles.serve.enabled=true"
    )
    assert set(claims(ds).values()) == {None}


def test_an_ingress_uses_the_class_and_issuer_and_routes_to_the_front():
    ds = docs(
        "roles.serve.enabled=true",
        "roles.serve.ingress.enabled=true",
        "roles.serve.ingress.host=node.example.org",
        files=SMALL,
    )
    ing = by("Ingress", "node-serve", ds)
    assert ing["spec"]["ingressClassName"] == "axiom"
    assert ing["metadata"]["annotations"]["cert-manager.io/cluster-issuer"] == "axiom-selfsigned"
    backend = ing["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    assert backend["name"] == "node-serve"  # the blue/green front, so a switch never touches it
    assert by("Service", "node-serve", ds)["metadata"]["annotations"]["axiom.io/front"] == "true"


def test_an_ingress_without_the_cluster_base_is_refused():
    out = helm(
        "roles.serve.enabled=true",
        "roles.serve.ingress.enabled=true",
        "roles.serve.ingress.host=h",
        check=False,
    )
    assert out.returncode != 0 and "needs contract.enabled" in out.stderr


def test_a_secret_comes_from_the_store_when_asked():
    ds = docs(
        "roles.serve.enabled=true",
        "roles.serve.secretFrom=serve-env",
        "roles.serve.secretFromStore=node/serve",
        files=SMALL,
    )
    es = by("ExternalSecret", "serve-env", ds)
    assert es["spec"]["secretStoreRef"] == {"kind": "ClusterSecretStore", "name": "axiom"}
    assert es["spec"]["dataFrom"] == [{"extract": {"key": "node/serve"}}]


def test_a_store_secret_without_the_cluster_base_is_refused():
    out = helm(
        "roles.serve.enabled=true",
        "roles.serve.secretFrom=s",
        "roles.serve.secretFromStore=k",
        check=False,
    )
    assert out.returncode != 0 and "needs contract.enabled" in out.stderr


def test_work_that_is_not_a_role_runs_as_a_service():
    ds = docs(
        "services.watcher.enabled=true",
        "services.watcher.command={axi,data,watch}",
        "services.watcher.storage=1Gi",
        files=SMALL,
    )
    sts = by("StatefulSet", "node-watcher", ds)
    assert sts["spec"]["template"]["spec"]["containers"][0]["args"] == ["axi", "data", "watch"]
    assert claims(ds)["node-watcher/data"] == "axiom-standard"


def test_a_service_with_a_port_can_be_blue_green_and_its_front_says_where_its_values_are():
    ds = docs(
        "services.gateway.enabled=true",
        "services.gateway.port=8700",
        "services.gateway.image.repository=example/gateway",
        "services.gateway.image.tag=1",
        "services.gateway.storage=1Gi",
        "services.gateway.blueGreen.enabled=true",
        "services.gateway.blueGreen.active=blue",
        "services.gateway.blueGreen.slots.blue.x=1",
        files=SMALL,
    )
    front = by("Service", "node-gateway", ds)
    assert front["metadata"]["annotations"]["axiom.io/values-path"] == "services.gateway"
    assert by("StatefulSet", "node-gateway-blue", ds)


def test_a_service_may_not_reuse_a_roles_name():
    out = helm("services.serve.enabled=true", check=False)
    assert out.returncode != 0 and "repeats a role's name" in out.stderr


def test_a_gpu_is_asked_for_the_same_way_everywhere():
    ds = docs("roles.model.enabled=true", "roles.model.gpu=1")
    spec = by("StatefulSet", "node-model-blue", ds)["spec"]["template"]["spec"]
    assert spec["runtimeClassName"] == "nvidia"
    assert spec["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == 1


def test_a_job_keeps_its_time_zone_and_can_use_a_services_volume():
    ds = docs(
        "services.store.enabled=true",
        "services.store.storage=1Gi",
        "jobs.nightly.schedule=0 5 * * *",
        "jobs.nightly.timeZone=America/Chicago",
        "jobs.nightly.command={axi,data,nightly}",
        "jobs.nightly.dataFrom=store",
    )
    cj = by("CronJob", "node-nightly", ds)
    assert cj["spec"]["timeZone"] == "America/Chicago"
    vol = cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]["volumes"][0]
    assert vol["persistentVolumeClaim"]["claimName"] == "data-node-store-0"


def test_a_disabled_job_is_not_rendered():
    """`default true false` is true in the template language, which used to
    render a job its values had switched off."""
    ds = docs("jobs.off.schedule=0 5 * * *", "jobs.off.command={true}", "jobs.off.enabled=false")
    assert not [d for d in ds if d["kind"] == "CronJob"]


def test_one_host_ignores_the_clusters_objects():
    ds = docs(
        "roles.serve.enabled=true",
        "roles.serve.secretFrom=s",
        "roles.serve.secretFromStore=k",
        "roles.serve.ingress.enabled=true",
        "roles.serve.ingress.host=h",
        files=SMALL,
    )
    c = compose_from_manifests(ds)
    assert "node-serve" in c["services"]


def test_one_host_refuses_a_time_zone_it_cannot_honour():
    ds = docs(
        "jobs.nightly.schedule=0 5 * * *",
        "jobs.nightly.timeZone=America/Chicago",
        "jobs.nightly.command={true}",
    )
    with pytest.raises(ValueError, match="UTC"):
        compose_from_manifests(ds)


def test_the_switch_changes_a_services_values_where_the_front_says(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def run(*a, check=True, cwd=None):
        calls.append(a)
        sets = [x for c in calls for x in c if "blueGreen.active=" in x]
        out = ""
        if "jsonpath={.metadata.annotations.axiom\\.io/active}" in a:
            out = "blue"
        elif "jsonpath={.metadata.annotations.axiom\\.io/values-path}" in a:
            out = "services.gateway"
        elif "jsonpath={.subsets[*].addresses[*].ip}" in a:
            out = "10.0.0.1 10.0.0.2" if sets and sets[-1].endswith("=both") else "10.0.0.2"
        return subprocess.CompletedProcess(a, 0, out, "")

    monkeypatch.setattr(bluegreen, "_run", run)
    monkeypatch.setattr(bluegreen, "_say", lambda *a, **k: None)
    monkeypatch.setattr(bluegreen.time, "sleep", lambda s: None)
    bluegreen.switch_k8s("node", "chart", [], "gateway", "green", settle=0)
    sets = [a[a.index("--set") + 1] for a in calls if "--set" in a]
    assert sets == [
        "services.gateway.blueGreen.active=blue",
        "services.gateway.blueGreen.active=both",
        "services.gateway.blueGreen.active=green",
    ]


SHARED = (
    "sharedVolumes.bronze.size=200Gi",
    "roles.serve.enabled=true",
    "roles.serve.mounts[0].volume=bronze",
    "roles.serve.mounts[0].path=/bronze",
    "services.conform.enabled=true",
    "services.conform.storage=1Gi",
    "services.conform.command={axi,data,conform-run}",
    "services.conform.mounts[0].volume=bronze",
    "services.conform.mounts[0].path=/bronze",
    "jobs.deposits.schedule=40 8 * * *",
    "jobs.deposits.command={sh,-c,exit 0}",
    "jobs.deposits.mounts[0].volume=bronze",
    "jobs.deposits.mounts[0].path=/bronze",
)


def test_a_shared_volume_is_one_claim_every_user_mounts_on_one_node():
    ds = docs(*SHARED, files=SMALL)
    pvc = by("PersistentVolumeClaim", "node-shared-bronze", ds)
    assert pvc["spec"]["storageClassName"] == "axiom-standard"
    assert pvc["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    for kind, name in (("StatefulSet", "node-serve-blue"), ("StatefulSet", "node-conform")):
        pod = by(kind, name, ds)["spec"]["template"]
        vols = {v["name"]: v for v in pod["spec"]["volumes"]}
        assert {"claimName": "node-shared-bronze"} in [
            v.get("persistentVolumeClaim") for v in vols.values()
        ]
        assert pod["metadata"]["labels"]["axiom.io/shared-volume"] == "bronze"
    job = by("CronJob", "node-deposits", ds)["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert job["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]


def test_a_mount_must_name_a_declared_shared_volume():
    out = helm(
        "roles.serve.enabled=true",
        "roles.serve.mounts[0].volume=nope",
        "roles.serve.mounts[0].path=/x",
        check=False,
    )
    assert out.returncode != 0 and '"nope" is not in sharedVolumes' in out.stderr


def test_files_from_a_secret_can_come_from_the_store_and_are_read_only():
    ds = docs(
        "roles.serve.enabled=true",
        "roles.serve.mounts[0].secret=gate-files",
        "roles.serve.mounts[0].path=/run/secrets/gate",
        "roles.serve.mounts[0].fromStore=site/gate",
        files=SMALL,
    )
    assert by("ExternalSecret", "gate-files", ds)["spec"]["dataFrom"] == [
        {"extract": {"key": "site/gate"}}
    ]
    c = by("StatefulSet", "node-serve-blue", ds)["spec"]["template"]["spec"]["containers"][0]
    m = next(v for v in c["volumeMounts"] if v["mountPath"] == "/run/secrets/gate")
    assert m["readOnly"] is True


def test_on_one_host_a_shared_volume_is_a_named_volume_and_secret_files_a_host_folder():
    ds = docs(
        *SHARED,
        "roles.serve.mounts[1].secret=gate-files",
        "roles.serve.mounts[1].path=/run/secrets/gate",
    )
    c = compose_from_manifests(ds)
    serve = c["services"]["node-serve-blue"]["volumes"]
    assert "node-shared-bronze:/bronze" in serve
    assert "${SERVE_GATE_FILES_DIR:-./secrets/gate-files}:/run/secrets/gate:ro" in serve
    assert "node-shared-bronze:/bronze" in c["services"]["node-conform"]["volumes"]


def test_several_roles_share_a_host_and_one_is_gated_by_another():
    ds = docs(
        "roles.serve.enabled=true",
        "roles.serve.ingress.enabled=true",
        "roles.serve.ingress.host=node.example.org",
        "roles.serve.ingress.paths={/hub,/ingest,/gate}",
        "roles.chat.enabled=true",
        "roles.chat.ingress.enabled=true",
        "roles.chat.ingress.host=node.example.org",
        "roles.chat.ingress.path=/",
        "roles.chat.ingress.forwardAuth.url=http://node-serve:8770/gate/verify",
        "roles.chat.ingress.forwardAuth.responseHeaders={X-User-Email}",
        files=SMALL,
    )
    serve = by("Ingress", "node-serve", ds)
    assert [p["path"] for p in serve["spec"]["rules"][0]["http"]["paths"]] == [
        "/hub",
        "/ingest",
        "/gate",
    ]
    chat = by("Ingress", "node-chat", ds)
    assert chat["metadata"]["annotations"][
        "traefik.ingress.kubernetes.io/router.middlewares"
    ].endswith("-node-chat-auth@kubernetescrd")
    mw = by("Middleware", "node-chat-auth", ds)
    assert mw["spec"]["forwardAuth"]["address"] == "http://node-serve:8770/gate/verify"
    # One host, one certificate secret.
    assert serve["spec"]["tls"][0]["secretName"] == chat["spec"]["tls"][0]["secretName"]


def test_every_container_that_installs_packages_gets_the_private_index_and_no_other_does():
    ds = docs(
        "packages.indexSecret.name=index",
        "packages.indexSecret.key=url",
        "roles.serve.enabled=true",
        "roles.chat.enabled=true",
        "jobs.nightly.schedule=0 5 * * *",
        "jobs.nightly.command={sh,-c,exit 0}",
    )
    env = lambda c: {e["name"]: e for e in c.get("env", [])}  # noqa: E731
    serve = by("StatefulSet", "node-serve-blue", ds)["spec"]["template"]["spec"]["containers"][0]
    assert env(serve)["PIP_EXTRA_INDEX_URL"]["valueFrom"] == {
        "secretKeyRef": {"name": "index", "key": "url"}
    }
    job = by("CronJob", "node-nightly", ds)["spec"]["jobTemplate"]["spec"]["template"]["spec"][
        "containers"
    ][0]
    assert "PIP_EXTRA_INDEX_URL" in env(job)
    chat = by("StatefulSet", "node-chat-blue", ds)["spec"]["template"]["spec"]["containers"][0]
    assert "PIP_EXTRA_INDEX_URL" not in env(chat)  # its own image installs nothing


def test_a_name_the_issuer_cannot_reach_uses_an_existing_certificate():
    ds = docs(
        "roles.serve.enabled=true",
        "roles.serve.ingress.enabled=true",
        "roles.serve.ingress.host=campus.example.org",
        "roles.serve.ingress.tlsSecret=campus-cert",
        files=SMALL,
    )
    ing = by("Ingress", "node-serve", ds)
    assert ing["spec"]["tls"][0]["secretName"] == "campus-cert"
    assert "cert-manager.io/cluster-issuer" not in ing["metadata"].get("annotations", {})


def test_the_private_index_can_come_from_the_store():
    ds = docs(
        "packages.indexSecret.name=private-index",
        "packages.indexSecret.key=URL",
        "packages.indexSecret.fromStore=site/private-index",
        files=SMALL,
    )
    assert by("ExternalSecret", "private-index", ds)["spec"]["dataFrom"] == [
        {"extract": {"key": "site/private-index"}}
    ]


def test_a_role_can_split_its_paths_into_routes_each_with_its_own_gate():
    ds = docs(
        "roles.serve.enabled=true",
        "roles.serve.ingress.enabled=true",
        "roles.serve.ingress.host=node.example.org",
        "roles.serve.ingress.routes[0].name=open",
        "roles.serve.ingress.routes[0].paths={/gate,/ingest}",
        "roles.serve.ingress.routes[1].name=gated",
        "roles.serve.ingress.routes[1].paths={/api,/app}",
        "roles.serve.ingress.routes[1].forwardAuth.url=http://node-serve:8770/gate/verify",
        files=SMALL,
    )
    open_, gated = by("Ingress", "node-serve-open", ds), by("Ingress", "node-serve-gated", ds)
    assert (
        "traefik.ingress.kubernetes.io/router.middlewares" not in open_["metadata"]["annotations"]
    )
    assert gated["metadata"]["annotations"][
        "traefik.ingress.kubernetes.io/router.middlewares"
    ].endswith("-node-serve-gated-auth@kubernetescrd")
    assert by("Middleware", "node-serve-gated-auth", ds)
    assert [p["path"] for p in gated["spec"]["rules"][0]["http"]["paths"]] == ["/api", "/app"]
