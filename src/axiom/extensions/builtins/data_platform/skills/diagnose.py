# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.diagnose`` — deterministic post-install health checks.

What this skill does:
- Validates the helm release exists.
- Confirms the bronze PVC is Bound.

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
from . import verify


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    namespace = params.get("namespace", "axiom-data")
    release = params.get("release", "axiom-data-platform")
    kube_context = params.get("kube_context")
    actor = params.get("actor")

    # ---- 0. extraction deps (local, no cluster needed) ------------------
    # Run BEFORE the kubectl guard: a missing OCR/PDF/office-doc lib silently
    # degrades ingestion to text-only or fails mid-run, and the operator must
    # hear about it even on a dev box without kubectl.
    extraction_check = verify.check_extraction_deps()
    extraction_finding = {
        "check": "extraction_deps",
        "ok": extraction_check.status is verify.Status.PASS,
        "detail": extraction_check.detail,
    }
    if extraction_check.remediation:
        extraction_finding["remediation"] = extraction_check.remediation

    if shutil.which("kubectl") is None:
        errors = ["kubectl not on PATH"]
        if not extraction_finding["ok"]:
            errors.append(verify.EXTRACTION_REMEDIATION)
        return SkillResult(
            ok=False,
            value={"findings": [extraction_finding]},
            errors=errors,
        )

    findings: list[dict[str, Any]] = [extraction_finding]
    actions: list[str] = []
    irregular = not extraction_finding["ok"]
    if irregular:
        actions.append(verify.EXTRACTION_REMEDIATION)

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
        capture_output=True,
        text=True,
        timeout=15,
    )
    if r.returncode != 0:
        return SkillResult(
            ok=False,
            errors=[f"helm release {release!r} not found in namespace {namespace!r}"],
        )
    try:
        info = json.loads(r.stdout)
        actions.append(f"helm release {release} status={info.get('info', {}).get('status', '?')}")
    except json.JSONDecodeError:
        actions.append(f"helm release {release} present (json parse failed)")

    # ---- 2. bronze PVC ---------------------------------------------------
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

    # ---- 3b. can each landing table refuse a duplicate? -----------------
    # A re-ingest is the documented recovery for every table the backup policy
    # marks re-derivable. If a landing table's only unique key is a sequence,
    # `ON CONFLICT DO NOTHING` conflicts with nothing and that recovery DOUBLES
    # the table instead of restoring it. An operator has to learn this before
    # the restore, not from the row count afterwards.
    try:
        from .._dsn import resolve_dsn
        from ..idempotency import audit

        dsn = resolve_dsn(params.get("dsn"))
    except Exception:  # noqa: BLE001 — a diagnose must not die resolving config
        dsn = None

    if not dsn:
        findings.append(
            {"check": "landing_table_dedup", "ok": True, "reason": "skipped: no DSN resolved"}
        )
        actions.append("landing-table dedup: skipped (no DSN)")
    else:
        try:
            import psycopg

            with psycopg.connect(dsn) as conn, conn.cursor() as cur:
                verdicts = audit(cur)
            for v in verdicts:
                actions.append(f"landing table {v}")
            unsafe = [v for v in verdicts if not v.safe]
            if unsafe:
                irregular = True
                for v in unsafe:
                    findings.append(
                        {
                            "check": "landing_table_dedup",
                            "ok": False,
                            "reason": f"{v.schema}.{v.table}: {v.reason}",
                        }
                    )
            else:
                findings.append(
                    {
                        "check": "landing_table_dedup",
                        "ok": True,
                        "reason": f"{len(verdicts)} landing table(s) can refuse a duplicate",
                    }
                )
        except Exception as exc:  # noqa: BLE001 — report, never raise out of diagnose
            # "Could not check" is NOT "checked and found a problem". An
            # unreachable database makes this verdict unknown, and escalating an
            # unknown to troubleshoot teaches operators the check cries wolf.
            findings.append(
                {
                    "check": "landing_table_dedup",
                    "ok": True,
                    "reason": f"skipped: could not inspect ({type(exc).__name__})",
                }
            )
            actions.append(f"landing-table dedup: skipped ({type(exc).__name__})")

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
