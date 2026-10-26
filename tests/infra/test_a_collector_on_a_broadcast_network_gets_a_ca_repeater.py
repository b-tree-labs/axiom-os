# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A collector that speaks Channel Access gets a repeater beside it, from the chart.

Channel Access clients rely on a per-host repeater to receive server beacons. The
Python client ships a Linux client library but no repeater binary, so every first
use printed `Unable to start "CA Repeater" process` and the client lost the beacon
that says an IOC came back (found 2026-10-22 reproducing a partner install).

The chart carries the repeater as a sidecar of the role that needs it. These tests
render the chart and hold the properties that make it safe: it shares the
collector's pod (and so its host network), it does not fight a repeater the host
already runs, it is small and unprivileged, and it cannot be turned on where it
would be useless.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "infra" / "charts" / "axiom-node"
PRODUCER = ["roles.producer.enabled=true", "roles.producer.command={neut,daq,run}"]


def render(*sets: str) -> list[dict]:
    if not shutil.which("helm"):
        pytest.skip("helm is not installed")
    args = ["helm", "template", "node", str(CHART)]
    for s in sets:
        args += ["--set", s]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def producer_pod(*sets: str) -> dict:
    for d in render(*PRODUCER, *sets):
        if d["kind"] in ("Deployment", "StatefulSet") and d["metadata"]["name"].endswith("producer"):
            return d["spec"]["template"]["spec"]
    raise AssertionError("no producer workload rendered")


ON = ["roles.producer.hostNetwork=true", "roles.producer.caRepeater.enabled=true"]


def test_off_by_default_so_nothing_else_changes():
    names = [c["name"] for c in producer_pod("roles.producer.hostNetwork=true")["containers"]]
    assert "ca-repeater" not in names


def test_enabled_adds_one_sidecar_in_the_collectors_pod_on_the_host_network():
    pod = producer_pod(*ON)
    names = [c["name"] for c in pod["containers"]]
    assert names.count("ca-repeater") == 1
    assert pod["hostNetwork"] is True, "beacons are broadcasts; the repeater must see the host's network"


def test_it_runs_the_repeater_directly_not_through_the_package_installer():
    c = next(c for c in producer_pod(*ON)["containers"] if c["name"] == "ca-repeater")
    script = " ".join(c["command"])
    assert "caRepeater" in script
    assert "axiom-entrypoint" not in script, "the repeater needs no packages and must not wait on the volume lock"
    assert not c.get("volumeMounts"), "it touches no data"


def test_it_does_not_fight_a_repeater_the_host_already_runs():
    """Two repeaters cannot share UDP 5065; the second exits, and a restart loop is an outage."""
    script = " ".join(next(c for c in producer_pod(*ON)["containers"] if c["name"] == "ca-repeater")["command"])
    assert "13C9" in script, "it checks whether UDP 5065 (0x13C9) is already bound before starting"
    assert "while true" in script, "it takes over if the other repeater goes away"


def test_it_is_small_and_unprivileged():
    c = next(c for c in producer_pod(*ON)["containers"] if c["name"] == "ca-repeater")
    sc = c["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False and sc["readOnlyRootFilesystem"] is True
    assert sc["capabilities"]["drop"] == ["ALL"]
    res = c["resources"]
    assert res["requests"]["memory"], "companions carry a request and no memory limit (spec-cicd-and-deployment)"


def test_it_is_restarted_if_nothing_is_listening_on_the_repeater_port():
    c = next(c for c in producer_pod(*ON)["containers"] if c["name"] == "ca-repeater")
    probe = " ".join(c["livenessProbe"]["exec"]["command"])
    assert "13C9" in probe


def test_it_cannot_be_enabled_where_it_would_be_useless():
    if not shutil.which("helm"):
        pytest.skip("helm is not installed")
    r = subprocess.run(
        ["helm", "template", "node", str(CHART), "--set", "roles.producer.enabled=true",
         "--set", "roles.producer.caRepeater.enabled=true"],
        capture_output=True, text=True,
    )
    assert r.returncode != 0
    assert "hostNetwork" in r.stderr
