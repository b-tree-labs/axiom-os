# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-182: the blue/green switch issues the same commands, in the same order, after the
switch was split into a slot driver (start the idle slot, route traffic) that the smoke-gated
switch (LOOP-S5) composes through the switch runner.

The command list is recorded, not run: what the zero-gap smoke proves on a real stack depends
on exactly this order (start, healthy, both, settle, new only), so the order itself is pinned.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from axiom.infra.deploy import bluegreen


class Recorder:
    def __init__(self, active: str = "blue") -> None:
        self.cmds: list[tuple[str, ...]] = []
        self.active = active
        self.endpoints = 1

    def __call__(self, *a, cwd=None, check=True):
        self.cmds.append(tuple(a))
        out = ""
        if a[:2] == ("kubectl", "get") and "axiom\\.io/active" in a[-1]:
            out = self.active
        elif a[:2] == ("kubectl", "get") and "values-path" in a[-1]:
            out = "roles.edge"
        elif a[:2] == ("kubectl", "get") and a[2] == "endpoints":
            out = " ".join(["10.0.0.1"] * self.endpoints)
        elif "ps" in a and "-q" in a:
            out = "cid"
        elif a[:2] == ("docker", "inspect"):
            out = "healthy"
        elif a[:2] == ("helm", "upgrade"):
            sets = [x for x in a if "blueGreen.active=" in x]
            self.endpoints = 2 if sets and sets[-1].endswith("=both") else 1
        return subprocess.CompletedProcess(list(a), 0, out, "")


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(bluegreen, "_run", r)
    monkeypatch.setattr(bluegreen.time, "sleep", lambda s: None)
    return r


def _compose(tmp_path: Path) -> Path:
    doc = {
        "services": {
            "node-serve": {"x-axiom-front": {"role": "serve", "release": "node", "port": 8770,
                                             "slots": ["blue", "green"]}},
            "node-serve-blue": {},
            "node-serve-green": {},
        }
    }
    (tmp_path / "compose.yaml").write_text(yaml.safe_dump(doc))
    return tmp_path


def _shape(cmds):
    out = []
    for c in cmds:
        if c[0] == "docker" and "up" in c:
            out.append(("up", c[-1]))
        elif c[0] == "docker" and "exec" in c:
            out.append(("route", c[-1].split("printf '")[1].split("\\n'")[0]))
        elif c[0] == "helm":
            out.append(("helm", [x for x in c if "blueGreen.active=" in x][0]))
        elif c[:2] == ("kubectl", "rollout"):
            out.append(("rollout", c[3]))
    return out


def test_compose_starts_the_idle_slot_then_routes_both_then_the_new_one_only(tmp_path, rec):
    bluegreen.switch_compose(_compose(tmp_path), "node", "serve", "green", settle=0)
    assert _shape(rec.cmds) == [
        ("up", "node-serve-green"),
        ("route", "server node-serve-blue:8770 resolve;\\nserver node-serve-green:8770 resolve;"),
        ("route", "server node-serve-green:8770 resolve;"),
    ]


def test_kubernetes_adds_the_idle_slot_at_the_current_active_then_both_then_the_new_one(rec):
    bluegreen.switch_k8s("node", "chart", ["-f", "v.yaml"], "edge", "green", settle=0)
    assert _shape(rec.cmds) == [
        ("helm", "roles.edge.blueGreen.active=blue"),
        ("rollout", "statefulset/node-edge-green"),
        ("helm", "roles.edge.blueGreen.active=both"),
        ("helm", "roles.edge.blueGreen.active=green"),
    ]


def test_kubernetes_does_nothing_when_the_slot_is_already_active(rec):
    rec.active = "green"
    bluegreen.switch_k8s("node", "chart", [], "edge", "green", settle=0)
    assert not [c for c in rec.cmds if c[0] == "helm"]
