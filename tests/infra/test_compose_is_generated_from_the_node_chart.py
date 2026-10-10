# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A single host runs Compose generated from the node chart, never a second hand-written copy.

Before this, the same roles were defined four times (systemd + Caddy, a Compose
file with nginx, a second container variant, and a collector container), and
they had already drifted: different source names, different key paths. The
chart is the one definition (ADR-170); Compose is derived from what it renders.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from axiom.infra.deploy.compose_from_chart import HEADER, compose_from_manifests

CHART = Path(__file__).resolve().parents[2] / "infra" / "charts" / "axiom-node"


def render(*sets: str) -> list[dict]:
    if not shutil.which("helm"):
        pytest.skip("helm is not installed")
    args = ["helm", "template", "node", str(CHART)]
    for s in sets:
        args += ["--set", s]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def test_an_edge_becomes_one_service_published_on_loopback_with_its_operator_folder():
    c = compose_from_manifests(
        render(
            "roles.edge.enabled=true",
            "roles.edge.configFrom=edge-config",
            "roles.edge.downstream=@platform:org",
        )
    )
    # Blue/green by default: the front publishes the port, a slot runs the edge.
    assert c["services"]["node-edge"]["ports"] == ["127.0.0.1:8787:8787"]
    svc = c["services"]["node-edge-blue"]
    assert svc["restart"] == "unless-stopped"
    assert svc["command"] == ["edge-run", "8787"]
    assert svc["environment"]["AXIOM_EDGE_DOWNSTREAM"] == "@platform:org"
    assert "node-edge-data:/data" in svc["volumes"]
    assert "${EDGE_CONFIG_DIR:-./config/edge}:/config:ro" in svc["volumes"]
    assert "node-edge-blue-pkg:/pkg" in svc["volumes"]
    assert svc["healthcheck"]["test"][0] == "CMD"
    assert svc["mem_limit"] == "1g" and svc["user"] == "10001"
    assert "node-edge-data" in c["volumes"]


def test_a_secret_is_a_file_the_operator_writes_from_the_vault_never_a_value():
    c = compose_from_manifests(
        render("roles.medallion.enabled=true", "roles.medallion.secretFrom=pg")
    )
    svc = c["services"]["node-medallion"]
    assert svc["env_file"] == ["${MEDALLION_SECRETS_ENV:-./secrets/medallion.env}"]
    assert "POSTGRES_PASSWORD" not in str(svc.get("environment", {}))
    assert svc["healthcheck"]["test"][:2] == ["CMD", "pg_isready"]


def test_host_networking_survives_for_protocols_that_need_broadcast():
    c = compose_from_manifests(
        render(
            "roles.producer.enabled=true",
            "roles.producer.hostNetwork=true",
            "roles.producer.command={neut,daq,run}",
        )
    )
    assert c["services"]["node-producer"]["network_mode"] == "host"


def test_a_profile_sizes_every_role_for_old_hardware():
    c = compose_from_manifests(render("profile=lowpower", "roles.edge.enabled=true"))
    assert c["services"]["node-edge-blue"]["mem_limit"] == "384m"


def test_a_scheduled_pull_becomes_a_loop_at_the_same_cadence():
    c = compose_from_manifests(
        render(
            "roles.edgePull.enabled=true",
            "roles.edgePull.connector=partner-edge",
            "roles.edgePull.schedule=*/5 * * * *",
        )
    )
    cmd = c["services"]["node-edge-pull"]["command"]
    assert cmd[:2] == ["sh", "-c"] and "sleep 300" in cmd[2] and "edge-pull partner-edge" in cmd[2]


def test_an_unknown_kind_is_refused_rather_than_silently_dropped():
    with pytest.raises(ValueError, match="DaemonSet"):
        compose_from_manifests([{"kind": "DaemonSet", "metadata": {"name": "x"}}])


def test_the_realtime_path_outranks_storage_on_one_host():
    c = compose_from_manifests(
        render(
            "roles.producer.enabled=true",
            "roles.medallion.enabled=true",
            "roles.medallion.secretFrom=pg",
        )
    )
    prod, db = c["services"]["node-producer"], c["services"]["node-medallion"]
    assert prod["cpu_shares"] > db["cpu_shares"]
    assert prod["oom_score_adj"] < db["oom_score_adj"]


def test_the_realtime_path_outranks_storage_on_kubernetes():
    docs = render(
        "roles.producer.enabled=true",
        "roles.medallion.enabled=true",
        "roles.medallion.secretFrom=pg",
    )
    classes = {d["metadata"]["name"]: d["value"] for d in docs if d["kind"] == "PriorityClass"}
    pods = {
        d["metadata"]["name"]: d["spec"]["template"]["spec"]
        for d in docs
        if d["kind"] == "StatefulSet"
    }
    assert (
        classes[pods["node-producer"]["priorityClassName"]]
        > classes[pods["node-medallion"]["priorityClassName"]]
    )
    lim = pods["node-producer"]["containers"][0]["resources"]
    assert lim["requests"] == lim["limits"], "the realtime collector gets guaranteed resources"


def test_turning_on_the_twin_is_the_only_change_on_bigger_hardware():
    base = [
        "roles.producer.enabled=true",
        "roles.producer.command={neut,daq,run}",
        "roles.medallion.enabled=true",
        "roles.medallion.secretFrom=pg",
        "roles.twin.command={python,/config/twin.py}",
    ]
    off = render(*base)
    on = render(*base, "roles.twin.enabled=true")

    def key(d):
        return (d["kind"], d["metadata"]["name"])

    off_docs, on_docs = {key(d): d for d in off}, {key(d): d for d in on}
    assert set(on_docs) - set(off_docs) == {("StatefulSet", "node-twin")}
    assert all(on_docs[k] == off_docs[k] for k in off_docs)
    env = {
        e["name"]: e["value"]
        for e in on_docs[("StatefulSet", "node-twin")]["spec"]["template"]["spec"]["containers"][0][
            "env"
        ]
    }
    assert env["AXIOM_REALTIME_URL"] == "http://node-producer:9100"


def test_a_twin_beside_a_host_networked_collector_reads_it_on_the_host():
    c = compose_from_manifests(
        render(
            "roles.producer.enabled=true",
            "roles.producer.hostNetwork=true",
            "roles.twin.enabled=true",
        )
    )
    twin = c["services"]["node-twin"]
    assert twin["network_mode"] == "host"
    assert twin["environment"]["AXIOM_REALTIME_URL"] == "http://127.0.0.1:9100"


def test_a_role_installs_its_own_packages():
    c = compose_from_manifests(
        render(
            "roles.producer.enabled=true",
            "roles.producer.packages.pin=site-collector[epics]==1.2.0",
            "roles.producer.packages.version=collector-1.2.0",
            "roles.edge.enabled=true",
        )
    )
    assert (
        c["services"]["node-producer"]["environment"]["AXIOM_PACKAGES"]
        == "site-collector[epics]==1.2.0"
    )
    assert c["services"]["node-edge-blue"]["environment"]["AXIOM_PACKAGES"] == "axiom-os-lm==0.68.0"


def test_a_companion_runs_beside_its_role_and_shares_its_volume():
    c = compose_from_manifests(
        render(
            "roles.edge.enabled=true",
            "roles.edge.companions[0].name=conform",
            "roles.edge.companions[0].command={sh,-c,axi data conform-run; sleep 86400}",
        )
    )
    comp = c["services"]["node-edge-blue-conform"]
    assert comp["command"] == ["sh", "-c", "axi data conform-run; sleep 86400"]
    assert "node-edge-data:/data" in comp["volumes"] and "ports" not in comp
    assert comp["restart"] == "unless-stopped"


def test_an_init_step_runs_once_before_its_role():
    c = compose_from_manifests(
        render("roles.edge.enabled=true", "roles.edge.init={axi,data,ensure-schema}")
    )
    init, main = c["services"]["node-edge-blue-init"], c["services"]["node-edge-blue"]
    assert init["restart"] == "no" and init["command"] == ["axi", "data", "ensure-schema"]
    assert main["depends_on"] == {
        "node-edge-blue-init": {"condition": "service_completed_successfully"}
    }


def test_what_a_node_reaches_and_listens_on_is_declared_for_the_disclosure():
    c = compose_from_manifests(
        render(
            "node.role=daq",
            "node.functions={acquire,transmit}",
            "node.agents=none",
            "roles.producer.enabled=true",
            "roles.producer.hostNetwork=true",
            "roles.producer.egress[0].host=intake.example.org",
            "roles.producer.egress[0].port=443",
            "roles.producer.egress[0].purpose=send readings",
            "roles.producer.egress[0].data=readings with units",
            "roles.producer.listen[0].address=127.0.0.1:9100",
            "roles.producer.listen[0].purpose=realtime point",
            "roles.edge.enabled=true",
            "roles.edge.egress[0].host=pypi.org",
            "roles.edge.egress[0].port=443",
            "roles.edge.egress[0].purpose=packages",
            "roles.edge.egress[0].data=none",
        )
    )
    assert c["x-axiom-node"] == {
        "role": "daq",
        "functions": ["acquire", "transmit"],
        "agents": "none",
        "maintenance": "off",
    }
    hosts = {(e["host"], e["port"]) for e in c["x-axiom-egress"]}
    assert hosts == {("intake.example.org", 443), ("pypi.org", 443)}
    assert c["services"]["node-producer"]["x-axiom-listen"] == [
        {"address": "127.0.0.1:9100", "purpose": "realtime point"}
    ]


def test_the_archive_role_sets_up_its_schema_after_its_database_and_inherits_its_settings():
    if not shutil.which("helm"):
        pytest.skip("helm is not installed")
    out = subprocess.run(
        [
            "helm",
            "template",
            "node",
            str(CHART),
            "-f",
            str(CHART / "values-lowpower.yaml"),
            "-f",
            str(CHART / "values-archive.yaml"),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    c = compose_from_manifests([d for d in yaml.safe_load_all(out) if d])
    init, conform = c["services"]["node-edge-blue-init"], c["services"]["node-edge-blue-conform"]
    for svc in (init, conform):
        assert svc["environment"]["AXIOM_WAIT_FOR"] == "node-medallion:5432"
        assert "AXIOM_SILVER_RETAIN_DAYS" in svc["environment"]
    assert (
        c["services"]["node-edge-blue"]["depends_on"]["node-edge-blue-init"]["condition"]
        == "service_completed_successfully"
    )
    # A companion waits for init too, as it does inside a Kubernetes pod.
    assert (
        conform["depends_on"]["node-edge-blue-init"]["condition"]
        == "service_completed_successfully"
    )
    assert "node-medallion" in c["services"]


SETUP_COMPOSE = (
    Path(__file__).resolve().parents[2] / "src" / "axiom" / "setup" / "docker-compose.yml"
)


def _dev_db() -> dict:
    if not shutil.which("helm"):
        pytest.skip("helm is not installed")
    out = subprocess.run(
        ["helm", "template", "node", str(CHART), "-f", str(CHART / "values-dev-db.yaml")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return compose_from_manifests([d for d in yaml.safe_load_all(out) if d])


def test_the_dev_database_keeps_an_existing_installs_names_and_data():
    db = _dev_db()["services"]["postgres"]
    assert db["container_name"] == "axiom-postgres"
    assert "mem_limit" not in db and "cpus" not in db
    assert "axiom-pgdata:/var/lib/postgresql/data" in db["volumes"]
    assert db["environment"]["PGDATA"] == "/var/lib/postgresql/data"
    assert db["shm_size"] == "1g"
    assert db["ports"] == ["5432:5432"]


def test_a_secret_key_is_read_from_the_callers_environment_never_written_down():
    db = _dev_db()["services"]["postgres"]
    assert db["environment"]["POSTGRES_PASSWORD"].startswith("${AXIOM_PG_PASSWORD:?")
    assert "env_file" not in db


def test_the_shipped_setup_compose_is_the_generated_one():
    """`axi infra` runs this file from the installed wheel, where helm may be absent, so it is
    committed; this keeps it equal to the chart's output."""
    expected = HEADER + yaml.safe_dump(_dev_db(), sort_keys=False)
    assert SETUP_COMPOSE.read_text() == expected, (
        "regenerate: python -m axiom.infra.deploy.compose_from_chart --chart infra/charts/axiom-node "
        "-f infra/charts/axiom-node/values-dev-db.yaml -o src/axiom/setup/docker-compose.yml"
    )


def test_the_archive_role_mounts_partner_config_and_gives_conform_real_cpu():
    if not shutil.which("helm"):
        pytest.skip("helm is not installed")
    args = [
        "helm",
        "template",
        "node",
        str(CHART),
        "-f",
        str(CHART / "values-lowpower.yaml"),
        "-f",
        str(CHART / "values-archive.yaml"),
    ]
    for s_ in (
        "roles.edge.extraConfig[0].name=site-maps",
        "roles.edge.extraConfig[0].from=site-maps",
        "roles.edge.extraConfig[0].path=/data/site-maps",
    ):
        args += ["--set", s_]
    docs = [
        d
        for d in yaml.safe_load_all(
            subprocess.run(args, capture_output=True, text=True, check=True).stdout
        )
        if d
    ]
    svcs = compose_from_manifests(docs)["services"]
    edge = svcs["node-edge-blue"]
    assert "${EDGE_CONFIG_DIR:-./config/edge}:/config:ro" in edge["volumes"]
    assert "${EDGE_SITE_MAPS_DIR:-./config/edge-site-maps}:/data/site-maps:ro" in edge["volumes"]
    conform = next(v for k, v in svcs.items() if k.endswith("conform"))
    assert conform["cpus"] == 0.5
    assert "AXI_STATE_DIR=/data/state" in " ".join(conform["command"])
    assert "${EDGE_SITE_MAPS_DIR:-./config/edge-site-maps}:/data/site-maps:ro" in conform["volumes"]


def test_a_shell_loop_in_a_command_survives_compose_interpolation(tmp_path):
    """Compose interpolates `$`; Kubernetes does not. Unescaped, the conform
    companion's `$d` rendered as "" and the loop skipped every source silently."""
    if not shutil.which("helm") or not shutil.which("docker"):
        pytest.skip("helm and docker are needed")
    args = [
        "helm",
        "template",
        "node",
        str(CHART),
        "-f",
        str(CHART / "values-lowpower.yaml"),
        "-f",
        str(CHART / "values-archive.yaml"),
    ]
    docs = [
        d
        for d in yaml.safe_load_all(
            subprocess.run(args, capture_output=True, text=True, check=True).stdout
        )
        if d
    ]
    compose = compose_from_manifests(docs)
    (tmp_path / "compose.yaml").write_text(yaml.safe_dump(compose))
    (tmp_path / "secrets").mkdir()
    for svc in compose["services"].values():
        for ef in svc.get("env_file", []):
            (tmp_path / ef.split(":-", 1)[1].rstrip("}")).touch()
    r = subprocess.run(
        ["docker", "compose", "config", "--no-path-resolution", "--format", "json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "POSTGRES_PASSWORD": "x"},
    )
    assert r.returncode == 0, r.stderr[-500:]
    rendered = __import__("json").loads(r.stdout)["services"]
    conform = next(v for k, v in rendered.items() if k.endswith("conform"))
    # `config` prints a re-loadable file, so a literal `$` comes back as `$$`;
    # unescaped, the variable was interpolated away and this reads `""`.
    assert '"$$d"' in " ".join(conform["command"])


def test_the_runtime_image_tag_changes_when_its_files_do():
    import hashlib

    image = CHART.parents[1] / "images" / "axiom-runtime"
    tag, digest = (image / "TAG").read_text().splitlines()[-1].split()
    now = hashlib.sha256(
        b"".join((image / f).read_bytes() for f in ("Dockerfile", "entrypoint.sh", "edge-run.sh"))
    ).hexdigest()
    assert now == digest, (
        "the runtime image's files changed: give it a new tag in values.yaml and in "
        f"infra/images/axiom-runtime/TAG ('<tag> {now}')"
    )
    assert yaml.safe_load((CHART / "values.yaml").read_text())["image"]["tag"] == tag


def _archive_compose():
    args = [
        "helm",
        "template",
        "node",
        str(CHART),
        "-f",
        str(CHART / "values-lowpower.yaml"),
        "-f",
        str(CHART / "values-archive.yaml"),
    ]
    docs = [
        d
        for d in yaml.safe_load_all(
            subprocess.run(args, capture_output=True, text=True, check=True).stdout
        )
        if d
    ]
    return docs, compose_from_manifests(docs)["services"]


def test_the_archive_role_declares_no_agents_to_every_process_it_runs():
    """C-48: the declaration reaches the processes, not only the disclosure."""
    if not shutil.which("helm"):
        pytest.skip("helm is not installed")
    _, svcs = _archive_compose()
    axiom = {k: v for k, v in svcs.items() if v["image"].split(":")[0].endswith("axiom-runtime")}
    assert {"node-edge-blue", "node-edge-blue-init", "node-edge-blue-conform"} <= set(axiom)
    for name, svc in axiom.items():
        assert 'agents = "none"' in svc["environment"]["AXIOM_NODE_TOML"], name
        assert svc["environment"]["AXIOM_NODE_CONFIG"]


def test_the_archive_edge_watches_the_database_volume_on_both_targets():
    if not shutil.which("helm"):
        pytest.skip("helm is not installed")
    docs, svcs = _archive_compose()
    assert "node-medallion-data:/watch/database:ro" in svcs["node-edge-blue"]["volumes"]
    assert svcs["node-edge-blue"]["environment"]["AXIOM_DISK_WATCH"] == "database=/watch/database"
    edge = next(
        d for d in docs if d["kind"] == "StatefulSet" and d["metadata"]["name"] == "node-edge-blue"
    )
    vol = next(
        v for v in edge["spec"]["template"]["spec"]["volumes"] if v["name"] == "watch-database"
    )
    assert vol["persistentVolumeClaim"] == {"claimName": "data-node-medallion-0", "readOnly": True}


def test_every_runtime_container_starts_in_its_data_folder_on_both_targets():
    """A collector's journal defaults to a path relative to where it starts.

    Started in `/` as an unprivileged user, `neut daq run` crash-looped with
    `Permission denied: 'daq-journal'` on the W10 replica. Every container the
    runtime image runs (role, init step, companion) starts in /data, its own
    writable volume, on Kubernetes and on Compose alike.
    """
    docs = render(
        "roles.producer.enabled=true",
        "roles.producer.command={neut,daq,run}",
        "roles.edge.enabled=true",
        "roles.edge.init={axi,data,ensure-schema}",
        "roles.edge.companions[0].name=conform",
        "roles.edge.companions[0].command={sh,-c,true}",
    )
    seen = 0
    for d in docs:
        if d.get("kind") not in ("StatefulSet", "Deployment"):
            continue
        spec = d["spec"]["template"]["spec"]
        for c in list(spec.get("initContainers") or []) + list(spec["containers"]):
            if "axiom-runtime" in c["image"]:
                assert c.get("workingDir") == "/data", (d["metadata"]["name"], c["name"])
                seen += 1
    assert seen >= 4
    services = compose_from_manifests(docs)["services"]
    for name in (
        "node-producer",
        "node-edge-blue",
        "node-edge-blue-init",
        "node-edge-blue-conform",
    ):
        assert services[name]["working_dir"] == "/data", name


def test_the_runtime_image_carries_rclone_for_the_box_fallback():
    """`axi data forward --box-remote` delivers through rclone when the intake is down.

    Every role runs this one image, so without rclone in it the Box fallback can
    never work on a Docker or Kubernetes node (found on a partner-site replica, W10). The
    CI build of the image runs `rclone version`, so a missing binary fails there.
    """
    image = CHART.parents[1] / "images" / "axiom-runtime"
    dockerfile = (image / "Dockerfile").read_text()
    assert "rclone" in dockerfile and "rclone version" in dockerfile
