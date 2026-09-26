# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""TDD tests for ``observability.diagnose`` skill."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from axiom.extensions.builtins.observability.skills import diagnose


def _proc(rc=0, out=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr="")


def _deploy_json(ready: int, want: int) -> str:
    return json.dumps({"status": {"readyReplicas": ready, "replicas": want}})


def _sts_json(ready: int, want: int) -> str:
    return json.dumps({"status": {"readyReplicas": ready, "replicas": want}})


def test_kubectl_missing(skill_ctx):
    with patch("shutil.which", return_value=None):
        r = diagnose.run({}, skill_ctx)
    assert not r.ok


def test_helm_release_missing(skill_ctx):
    def fake_run(cmd, **kw):
        if cmd[0] == "helm":
            return _proc(rc=1)
        return _proc(out="")
    with patch("shutil.which", return_value="/bin/x"), \
         patch("subprocess.run", side_effect=fake_run):
        r = diagnose.run({"release": "obs", "namespace": "obs"}, skill_ctx)
    assert not r.ok
    assert any("release" in e for e in r.errors)


def test_all_healthy(skill_ctx):
    def fake_run(cmd, **kw):
        if cmd[0] == "helm":
            return _proc(out=json.dumps({"info": {"status": "deployed"}}))
        # kubectl get deploy / sts
        if "get" in cmd and "deploy" in cmd:
            return _proc(out=_deploy_json(1, 1))
        if "get" in cmd and ("sts" in cmd or "statefulset" in cmd):
            return _proc(out=_sts_json(1, 1))
        return _proc(out="")
    with patch("shutil.which", return_value="/bin/x"), \
         patch("subprocess.run", side_effect=fake_run):
        r = diagnose.run({"release": "obs", "namespace": "obs"}, skill_ctx)
    assert r.ok, r.errors
    findings = r.value["findings"]
    assert all(f["ok"] for f in findings)
    names = {f["check"] for f in findings}
    assert "deploy_web" in names
    assert "deploy_worker" in names
    assert "sts_postgres" in names
    assert "sts_clickhouse" in names


def test_web_pod_not_ready_flags_irregular(skill_ctx):
    def fake_run(cmd, **kw):
        if cmd[0] == "helm":
            return _proc(out=json.dumps({"info": {"status": "deployed"}}))
        if "get" in cmd and "deploy" in cmd and any("web" in a for a in cmd):
            return _proc(out=_deploy_json(0, 1))
        if "get" in cmd and "deploy" in cmd:
            return _proc(out=_deploy_json(1, 1))
        if "get" in cmd and ("sts" in cmd or "statefulset" in cmd):
            return _proc(out=_sts_json(1, 1))
        return _proc(out="")
    with patch("shutil.which", return_value="/bin/x"), \
         patch("subprocess.run", side_effect=fake_run):
        r = diagnose.run({"release": "obs", "namespace": "obs"}, skill_ctx)
    assert not r.ok
    findings = r.value["findings"]
    web = next(f for f in findings if f["check"] == "deploy_web")
    assert not web["ok"]
