# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.troubleshoot`` — PLINTH-persona reasoning over an irregularity.

This is the *LLM-mediated* skill in the install pipeline. Per
``feedback_tidy_trust_and_llm_judgment``: deterministic floors UNDER
LLM judgment. ``data.diagnose`` is the floor (yes/no per check); this
skill is the judgment ("what does it MEAN that the daemon isn't
Ready?").

The skill pulls evidence via other registry skills (bidirectional A2A):

- ``axi.log.tail`` (future) — recent logs from the offending pod
- ``axi.capabilities.probe`` — re-check cluster prerequisites
- ``rag.retrieve`` — pull architecture-doc context to ground reasoning

Then it composes a prompt for the PLINTH persona and returns the
verdict + a proposed fix. **It does not auto-apply the fix** —
remediation routes back through deterministic skills (which apply
guarded_act).

For DP-1's first iteration the LLM call is a no-op stub: it returns
the raw findings + a "manual investigation required" verdict. The
hook is wired so the LLM bind-in is a small follow-up.
"""

from __future__ import annotations

import subprocess
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    namespace = params.get("namespace", "axiom-data")
    release = params.get("release", "axiom-data-platform")
    kube_context = params.get("kube_context")
    findings = params.get("findings", [])

    actions: list[str] = []
    evidence: dict[str, Any] = {}

    # ---- gather evidence from failed checks -----------------------------
    for f in findings:
        if f.get("ok"):
            continue
        check = f.get("check", "")
        if check.startswith("deploy_"):
            role = check.split("_", 1)[1]
            pod_logs = _pod_logs(release, role, namespace, kube_context)
            evidence[check] = {"logs_tail": pod_logs[-2000:]}
            actions.append(f"collected last 2KB of logs for {release}-dagster-{role}")
        elif check == "bronze_pvc":
            ev = _pvc_describe(f"{release}-bronze", namespace, kube_context)
            evidence[check] = {"describe": ev[-2000:]}
            actions.append(f"described pvc {release}-bronze")

    # ---- LLM reasoning hook (stub for DP-1 v1) --------------------------
    # When the PLINTH persona is wired:
    #   verdict = ctx.registry.invoke("axi.chat.persona", {
    #       "persona": "plinth",
    #       "system": "You are PLINTH, the data-platform steward. Reason
    #                  about why this install check failed.",
    #       "user_context": {"findings": findings, "evidence": evidence},
    #   }, ctx)
    #
    # For now: deterministic verdict + the evidence dict so an operator
    # (or a downstream LLM bound after merge) can act on it.
    verdict = {
        "summary": (
            "PLINTH persona LLM reasoning not wired yet — "
            "see findings + evidence for manual investigation."
        ),
        "next_steps": _next_steps_for(findings),
    }

    return SkillResult(
        ok=True,  # the troubleshoot skill itself succeeds; the *install*
                  # is the one that's failing — and diagnose already
                  # reported ok=False up the chain.
        value={
            "findings": findings,
            "evidence": evidence,
            "verdict": verdict,
        },
        actions_taken=actions + ["composed troubleshoot report"],
    )


# ---- evidence collectors -------------------------------------------------


def _pod_logs(release: str, role: str, namespace: str, kube_context: str | None) -> str:
    """Return the tail of the most recent pod's logs for a role."""
    selector = f"app.kubernetes.io/instance={release},role={role}"
    cmd = ["kubectl", "-n", namespace, "logs", "-l", selector, "--tail=200"]
    if kube_context:
        cmd = ["kubectl", "--context", kube_context, *cmd[1:]]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return (r.stdout or r.stderr or "")[:4000]


def _pvc_describe(name: str, namespace: str, kube_context: str | None) -> str:
    cmd = ["kubectl", "-n", namespace, "describe", "pvc", name]
    if kube_context:
        cmd = ["kubectl", "--context", kube_context, *cmd[1:]]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return (r.stdout or r.stderr or "")[:4000]


def _next_steps_for(findings: list[dict]) -> list[str]:
    """Heuristic suggestions per failure mode, until the LLM is wired."""
    steps: list[str] = []
    for f in findings:
        if f.get("ok"):
            continue
        check = f.get("check", "")
        if check == "deploy_webserver":
            steps.append(
                "Webserver Deployment is not Ready: check `kubectl describe deploy "
                "<release>-dagster-webserver` for image-pull errors or pip-install "
                "failures in the initContainer (PyPI reachable?)."
            )
        elif check == "deploy_daemon":
            steps.append(
                "Daemon Deployment is not Ready: same image + initContainer dependencies "
                "as the webserver; if webserver is up, suspect a per-process resource "
                "constraint or a CrashLoopBackOff."
            )
        elif check == "bronze_pvc":
            steps.append(
                "Bronze PVC is not Bound: check `kubectl get sc` for an available "
                "storage class, and `kubectl describe pvc` for the binding error."
            )
    if not steps:
        steps.append(
            "No specific heuristic matched. Run `axi data diagnose --verbose` "
            "and share the findings + evidence with a maintainer."
        )
    return steps
