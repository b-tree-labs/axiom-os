# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``connector presence-deploy`` — deploy a per-principal Axi pod (ADR-074 A2).

Renders/installs the per-principal presence chart so "Ben's Axi" runs always-on
in the cluster (one pod per human, own ServiceAccount + secret scope) and
survives the laptop being off. Mirrors data_platform/skills/install.py's helm
pattern; cluster-agnostic (pass ``--kube-context``) so the same chart targets
a shared HPC cluster later. Builds the command/values by default; only executes helm when asked.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillResult

from ..presence import principal_slug

_CHART = Path(__file__).resolve().parent.parent / "deploy" / "helm"


def _helm_argv(action: str, slug: str, namespace: str, sets: dict[str, str],
               kube_context: str | None) -> list[str]:
    argv = ["helm", action]
    if action == "upgrade":
        argv.append("--install")
    argv += [slug, str(_CHART), "--namespace", namespace]
    if kube_context:
        argv += ["--kube-context", kube_context]
    for k, v in sets.items():
        argv += ["--set", f"{k}={v}"]
    return argv


def presence_deploy(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    principal = params.get("principal")
    if not principal:
        return SkillResult(ok=False, errors=["principal is required (@axi:<context>)"])
    slug = principal_slug(principal)
    namespace = params.get("namespace") or "axiom-data"
    sets = {
        "principal": principal,
        "slug": slug,
        "namespace": namespace,
        "channel": params.get("channel", ""),
        "accountableHuman": params.get("accountable_human", ""),
    }
    if params.get("image"):
        sets["image"] = params["image"]
    if params.get("secret_name"):
        sets["secretName"] = params["secret_name"]

    # Default is a dry render (helm template); execute upgrade only when asked.
    action = "upgrade" if params.get("apply") else "template"
    argv = _helm_argv(action, slug, namespace, sets, params.get("kube_context"))

    value: dict[str, Any] = {"release": slug, "namespace": namespace, "command": argv, "sets": sets}
    if params.get("apply") or params.get("runner"):
        runner: Callable[..., Any] = params.get("runner") or subprocess.run
        proc = runner(argv, capture_output=True, text=True)
        value["returncode"] = getattr(proc, "returncode", None)
        value["stdout"] = getattr(proc, "stdout", "")
        if getattr(proc, "returncode", 0) not in (0, None):
            return SkillResult(ok=False, errors=[getattr(proc, "stderr", "helm failed")], value=value)
    return SkillResult(ok=True, value=value)


__all__ = ["presence_deploy"]
