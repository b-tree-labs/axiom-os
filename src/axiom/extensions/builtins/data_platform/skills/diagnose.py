# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.diagnose`` — deterministic post-install health checks.

What this skill does:
- Validates the helm release exists.
- Confirms Dagster webserver + daemon Deployments are Ready.
- Confirms the bronze PVC is Bound.
- Confirms the init-job created the dagster meta DB.

What this skill DOES NOT do: reason about *why* a check failed. That's
``data.troubleshoot``'s job (LLM-mediated PLINTH persona reasoning).
Diagnose invokes troubleshoot on irregularity — bidirectional A2A
through the registry.

Per ``feedback_tidy_trust_and_llm_judgment``: deterministic floors
UNDER LLM judgment. Diagnose is the floor; troubleshoot is the
judgment.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from axiom.governance.classification import Classification
from axiom.infra.skills import SkillContext, SkillResult

from .. import _authz


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    namespace = params.get("namespace", "axiom-data")
    release = params.get("release", "axiom-data-platform")
    kube_context = params.get("kube_context")
    actor = params.get("actor")

    if shutil.which("kubectl") is None:
        return SkillResult(ok=False, errors=["kubectl not on PATH"])

    findings: list[dict[str, Any]] = []
    actions: list[str] = []
    irregular = False

    # Diagnose is read-only but still an audit-worthy action. The wrap
    # enters once at the top so the receipt chain shows the
    # install → diagnose handoff. Wrap doesn't change behavior or the
    # return path; receipt fragment id appears in actions_taken.
    with _authz.action(
        verb="diagnose",
        resource=f"data-platform://{namespace}",
        classification=Classification.INTERNAL,
        actor=actor,
    ) as _act:
        actions.append(f"audit-receipt: {_act.receipt_id}")

    def _kubectl(*args: str) -> subprocess.CompletedProcess[str]:
        cmd = ["kubectl", "-n", namespace, *args]
        if kube_context:
            cmd = ["kubectl", "--context", kube_context, "-n", namespace, *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    # ---- 1. release sanity ----------------------------------------------
    r = subprocess.run(
        ["helm", "status", release, "-n", namespace, "-o", "json"]
        + (["--kube-context", kube_context] if kube_context else []),
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0:
        return SkillResult(
            ok=False,
            errors=[f"helm release {release!r} not found in namespace {namespace!r}"],
        )
    try:
        info = json.loads(r.stdout)
        actions.append(
            f"helm release {release} status={info.get('info', {}).get('status', '?')}"
        )
    except json.JSONDecodeError:
        actions.append(f"helm release {release} present (json parse failed)")

    # ---- 2. webserver Deployment ----------------------------------------
    for role in ("webserver", "daemon"):
        dep = f"{release}-dagster-{role}"
        r = _kubectl("get", "deploy", dep, "-o", "json")
        if r.returncode != 0:
            findings.append({"check": f"deploy_{role}", "ok": False, "reason": "not found"})
            irregular = True
            continue
        try:
            d = json.loads(r.stdout)
            ready = d.get("status", {}).get("readyReplicas", 0) or 0
            want = d.get("status", {}).get("replicas", 0) or 0
            ok = ready == want and want > 0
            findings.append({
                "check": f"deploy_{role}", "ok": ok,
                "ready": ready, "want": want,
            })
            if not ok:
                irregular = True
            actions.append(f"deploy {dep}: {ready}/{want} ready")
        except json.JSONDecodeError:
            findings.append({"check": f"deploy_{role}", "ok": False, "reason": "json parse"})
            irregular = True

    # ---- 3. bronze PVC ---------------------------------------------------
    pvc = f"{release}-bronze"
    r = _kubectl("get", "pvc", pvc, "-o", "json")
    if r.returncode != 0:
        findings.append({"check": "bronze_pvc", "ok": False, "reason": "not found"})
        irregular = True
    else:
        try:
            p = json.loads(r.stdout)
            phase = p.get("status", {}).get("phase", "")
            ok = phase == "Bound"
            findings.append({"check": "bronze_pvc", "ok": ok, "phase": phase})
            if not ok:
                irregular = True
            actions.append(f"pvc {pvc}: phase={phase}")
        except json.JSONDecodeError:
            findings.append({"check": "bronze_pvc", "ok": False, "reason": "json parse"})
            irregular = True

    # ---- 4. invoke troubleshoot if irregular ----------------------------
    if irregular:
        actions.append("irregularity detected — invoking data.troubleshoot")
        tshoot = ctx.registry.invoke(
            "data.troubleshoot",
            {
                "namespace": namespace,
                "release": release,
                "kube_context": kube_context,
                "findings": findings,
            },
            ctx,
        )
        actions.extend(tshoot.actions_taken)
        return SkillResult(
            ok=False,
            value={"findings": findings, "troubleshoot": tshoot.value},
            actions_taken=actions,
            errors=[f"{sum(1 for f in findings if not f['ok'])} check(s) failed"],
        )

    return SkillResult(
        ok=True,
        value={"findings": findings},
        actions_taken=actions,
    )
