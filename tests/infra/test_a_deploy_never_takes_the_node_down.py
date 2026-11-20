# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Never down from our own causes: a role is deployed blue/green on every target.

Two slots of a role run side by side, sharing the role's data (the edge's
outbox is safe for two writers), each with its own installed packages. One
stable front sends traffic to the active slot. A switch goes blue -> both ->
green, so at no moment does the front have nowhere to send a request, and the
old slot keeps running for rollback.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from axiom.infra.deploy.bluegreen import upstream_lines
from axiom.infra.deploy.compose_from_chart import compose_from_manifests

CHART = Path(__file__).resolve().parents[2] / "infra" / "charts" / "axiom-node"


def render(*sets: str) -> list[dict]:
    if not shutil.which("helm"):
        pytest.skip("helm is not installed")
    args = ["helm", "template", "node", str(CHART)]
    for s in sets:
        args += ["--set", s]
    return [
        d
        for d in yaml.safe_load_all(
            subprocess.run(args, capture_output=True, text=True, check=True).stdout
        )
        if d
    ]


EDGE = ["roles.edge.enabled=true", "roles.edge.configFrom=edge-config"]
TWO = [*EDGE, "roles.edge.blueGreen.slots.green.packages.version=next"]


def by(docs, kind):
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == kind}


def test_each_slot_has_its_own_packages_and_shares_the_roles_data():
    sts = by(render(*TWO), "StatefulSet")
    assert set(sts) == {"node-edge-blue", "node-edge-green"}
    for s in sts.values():
        spec = s["spec"]["template"]["spec"]
        claims = [
            v["persistentVolumeClaim"]["claimName"]
            for v in spec["volumes"]
            if "persistentVolumeClaim" in v
        ]
        assert claims == ["node-edge-data"]
        assert [t["metadata"]["name"] for t in s["spec"]["volumeClaimTemplates"]] == ["pkg"]
        env = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
        assert env["AXIOM_ROOT"] == "/pkg"
    green = {
        e["name"]: e.get("value")
        for e in sts["node-edge-green"]["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert green["AXIOM_PACKAGES_VERSION"] == "next"


def test_the_front_selects_the_active_slot_and_a_preview_selects_the_other():
    svc = by(render(*TWO), "Service")
    assert svc["node-edge"]["spec"]["selector"]["axiom.io/slot"] == "blue"
    assert svc["node-edge-preview"]["spec"]["selector"]["axiom.io/slot"] == "green"


def test_during_a_switch_the_front_selects_both_slots():
    svc = by(render(*TWO, "roles.edge.blueGreen.active=both"), "Service")
    assert "axiom.io/slot" not in svc["node-edge"]["spec"]["selector"]
    assert "node-edge-preview" not in svc


def test_changing_the_active_slot_changes_only_the_selectors():
    a, b = render(*TWO), render(*TWO, "roles.edge.blueGreen.active=green")
    diff = [d["metadata"]["name"] for d in a if d not in b]
    assert set(diff) == {"node-edge", "node-edge-preview"}, "a switch must not restart any pod"


def test_on_one_host_a_front_publishes_the_port_and_routes_to_the_active_slot():
    c = compose_from_manifests(render(*TWO))
    front = c["services"]["node-edge"]
    assert front["ports"] == ["127.0.0.1:8787:8787"]
    assert front["x-axiom-front"] == {
        "role": "edge",
        "port": 8787,
        "active": "blue",
        "slots": ["blue", "green"],
        "release": "node",
    }
    for slot in ("blue", "green"):
        s = c["services"][f"node-edge-{slot}"]
        assert "ports" not in s
        assert (
            "node-edge-data:/data" in s["volumes"] and f"node-edge-{slot}-pkg:/pkg" in s["volumes"]
        )


def test_the_upstream_names_every_slot_that_should_receive_traffic():
    assert upstream_lines("node", "edge", 8787, "green") == ["server node-edge-green:8787 resolve;"]
    assert upstream_lines("node", "edge", 8787, "both", slots=["blue", "green"]) == [
        "server node-edge-blue:8787 resolve;",
        "server node-edge-green:8787 resolve;",
    ]


def test_the_fronts_nginx_config_is_well_formed():
    c = compose_from_manifests(render(*TWO))
    script = c["services"]["node-edge"]["command"][2]
    conf = script.split("printf '%s\\n' '", 1)[1].split("'", 1)[0]
    assert conf.count("{") == conf.count("}"), conf
    assert "}}" not in conf


def test_the_serving_tier_is_blue_green_too():
    docs = render(
        "roles.serve.enabled=true", "roles.serve.blueGreen.slots.green.packages.version=next"
    )
    assert {"node-serve-blue", "node-serve-green"} <= set(by(docs, "StatefulSet"))
    assert by(docs, "Service")["node-serve"]["spec"]["selector"]["axiom.io/slot"] == "blue"


def test_the_front_follows_a_slot_that_restarts_on_a_new_address():
    """nginx resolves an upstream name once at start unless told otherwise.

    On a partner-site replica (W10) the edge slot restarted and came back on a
    new address; the front kept proxying to the old one and answered 502 to
    the collector until it was restarted by hand. The front now asks Docker's
    DNS again (resolver + zone + `resolve`), and a front volume written before
    this gains `resolve` in place.
    """
    c = compose_from_manifests(render(*TWO))
    script = c["services"]["node-edge"]["command"][2]
    conf = script.split("printf '%s\\n' '", 1)[1].split("'", 1)[0]
    assert "resolver 127.0.0.11 valid=5s" in conf
    assert "upstream active { zone active 64k; include /front/upstream.conf; }" in conf
    assert "resolve;" in script.split("printf '%s\\n'", 1)[0], "the initial upstream lines resolve"
    assert "sed -i -E 's/^(server [^ ;]+);$$/\\1 resolve;/' /front/upstream.conf" in script


def test_rag_model_and_chat_are_blue_green_and_an_image_role_switches_by_tag():
    docs = render(
        "roles.rag.enabled=true",
        "roles.model.enabled=true",
        "roles.chat.enabled=true",
        "roles.model.blueGreen.slots.green.image.tag=next",
    )
    sets = by(docs, "StatefulSet")
    assert {"node-rag-blue", "node-model-blue", "node-model-green", "node-chat-blue"} <= set(sets)
    img = {n: s["spec"]["template"]["spec"]["containers"][0]["image"] for n, s in sets.items()}
    assert img["node-model-blue"].endswith(":0.1.0") and img["node-model-green"].endswith(":next")
    # A role from its own image gets no package environment and no package volume.
    model = sets["node-model-green"]["spec"]
    assert model["volumeClaimTemplates"] == []
    env = {e["name"] for e in model["template"]["spec"]["containers"][0].get("env") or []}
    assert not {"AXIOM_PACKAGES", "AXIOM_ROOT"} & env
    for front in ("node-rag", "node-model", "node-chat"):
        assert by(docs, "Service")[front]["spec"]["selector"]["axiom.io/slot"] == "blue"
    chat = sets["node-chat-blue"]["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e["value"] for e in chat["env"]}
    # The image's default engine loads sentence-transformers before the
    # process listens. The data volume covers that cache, so a fresh volume
    # downloads the model and the slot is OOM-killed before /health answers.
    assert env["RAG_EMBEDDING_ENGINE"] == "openai"


def test_a_collector_with_overlap_runs_under_the_switch_supervisor():
    docs = render(
        "roles.producer.enabled=true",
        "roles.producer.command={python,/config/c.py}",
        "roles.producer.overlap.enabled=true",
        "roles.producer.overlap.instanceEnv=SITE_INSTANCE",
    )
    args = by(docs, "StatefulSet")["node-producer"]["spec"]["template"]["spec"]["containers"][0][
        "args"
    ]
    assert args[:4] == ["python", "-m", "axiom.infra.switch", "--worker"]
    assert args[args.index("--instance-env") + 1] == "SITE_INSTANCE"
    assert args[args.index("--") + 1 :] == ["python", "/config/c.py"]


def test_scheduled_work_is_a_cronjob_that_never_runs_twice_at_once():
    docs = render(
        "roles.edge.enabled=true",
        "jobs.nightly.schedule=15 3 * * *",
        "jobs.nightly.command={axi,data,retention}",
        "jobs.nightly.dataFrom=edge",
    )
    job = by(docs, "CronJob")["node-nightly"]
    assert job["spec"]["concurrencyPolicy"] == "Forbid"
    vols = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]["volumes"]
    assert vols[0]["persistentVolumeClaim"]["claimName"] == "node-edge-data"


def test_uninstalling_the_release_keeps_the_slots_shared_data():
    """The shared volume holds the outbox: batches not yet delivered upstream.
    It is the one claim Helm owns, so Helm must be told to leave it."""
    (pvc,) = by(render(*TWO), "PersistentVolumeClaim").values()
    assert pvc["metadata"]["name"] == "node-edge-data"
    assert pvc["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"


def test_a_slot_that_watches_a_volume_can_be_scheduled_beside_it():
    """Found installing an archive node on k3d: the first edge slot stayed Pending
    ("didn't match pod affinity rules") once its database was running. A pod may
    satisfy a term that matches itself only while no running pod matches any of
    its terms, so a self term next to a watched role's term is never met for the
    first slot. Beside the watched roles only, every slot still shares one node."""
    docs = render(*TWO, "roles.medallion.enabled=true", "roles.medallion.secretFrom=db",
                  "roles.edge.watchVolumes[0].label=database", "roles.edge.watchVolumes[0].role=medallion")
    for s in by(docs, "StatefulSet").values():
        if s["metadata"]["name"] == "node-medallion":
            continue
        terms = s["spec"]["template"]["spec"]["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
        assert [t["labelSelector"]["matchLabels"]["app.kubernetes.io/component"] for t in terms] == ["medallion"]


def test_slots_that_watch_nothing_still_share_a_node():
    for s in by(render(*TWO), "StatefulSet").values():
        terms = s["spec"]["template"]["spec"]["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
        assert [t["labelSelector"]["matchLabels"]["app.kubernetes.io/component"] for t in terms] == ["edge"]
