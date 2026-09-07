# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``observe.diagnose`` — deterministic post-install health checks.

Walks the helm release, both Langfuse Deployments (web + worker), and
both backing StatefulSets (postgres + clickhouse). Returns ``ok=True``
when every probe lands green.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def _readiness(d: dict) -> tuple[bool, int, int]:
    ready = d.get("status", {}).get("readyReplicas", 0) or 0
    want = d.get("status", {}).get("replicas", 0) or 0
    return (ready == want and want > 0, ready, want)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    namespace = params.get("namespace", "axiom-observability")
    release = params.get("release", "axiom-observability")
    kube_context = params.get("kube_context")

    if shutil.which("kubectl") is None:
        return SkillResult(ok=False, errors=["kubectl not on PATH"])

    findings: list[dict[str, Any]] = []
    actions: list[str] = []
    irregular = False

    def _kubectl(*args: str) -> subprocess.CompletedProcess[str]:
        cmd: list[str] = ["kubectl"]
        if kube_context:
            cmd += ["--context", kube_context]
        cmd += ["-n", namespace, *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    # ---- 1. helm release sanity -----------------------------------------
    helm_cmd = ["helm", "status", release, "-n", namespace, "-o", "json"]
    if kube_context:
        helm_cmd += ["--kube-context", kube_context]
    r = subprocess.run(helm_cmd, capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        return SkillResult(
            ok=False,
            errors=[f"helm release {release!r} not found in namespace {namespace!r}"],
        )
    try:
        info = json.loads(r.stdout)
        status = info.get("info", {}).get("status", "?")
        actions.append(f"helm release {release} status={status}")
    except json.JSONDecodeError:
        actions.append(f"helm release {release} present (json parse failed)")

    # ---- 2. Deployments: langfuse-web + langfuse-worker -----------------
    for role in ("web", "worker"):
        dep = f"{release}-{role}"
        r = _kubectl("get", "deploy", dep, "-o", "json")
        if r.returncode != 0:
            findings.append({"check": f"deploy_{role}", "ok": False, "reason": "not found"})
            irregular = True
            continue
        try:
            d = json.loads(r.stdout)
            ok, ready, want = _readiness(d)
            findings.append({"check": f"deploy_{role}", "ok": ok,
                             "ready": ready, "want": want})
            if not ok:
                irregular = True
            actions.append(f"deploy {dep}: {ready}/{want} ready")
        except json.JSONDecodeError:
            findings.append({"check": f"deploy_{role}", "ok": False, "reason": "json parse"})
            irregular = True

    # ---- 3. StatefulSets: postgres + clickhouse -------------------------
    for role in ("postgres", "clickhouse"):
        sts = f"{release}-{role}"
        r = _kubectl("get", "sts", sts, "-o", "json")
        if r.returncode != 0:
            findings.append({"check": f"sts_{role}", "ok": False, "reason": "not found"})
            irregular = True
            continue
        try:
            d = json.loads(r.stdout)
            ok, ready, want = _readiness(d)
            findings.append({"check": f"sts_{role}", "ok": ok,
                             "ready": ready, "want": want})
            if not ok:
                irregular = True
            actions.append(f"sts {sts}: {ready}/{want} ready")
        except json.JSONDecodeError:
            findings.append({"check": f"sts_{role}", "ok": False, "reason": "json parse"})
            irregular = True

    if irregular:
        n_fail = sum(1 for f in findings if not f["ok"])
        return SkillResult(
            ok=False,
            value={"findings": findings},
            actions_taken=actions,
            errors=[f"{n_fail} check(s) failed"],
        )
    return SkillResult(ok=True, value={"findings": findings}, actions_taken=actions)
