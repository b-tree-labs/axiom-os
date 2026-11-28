# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``doctor.run``: the environment checks ``axi doctor`` runs, as data. Changes nothing.

``axi doctor`` also asks a model to explain the failures and, with ``--fix``, remediates. A
conversation gets the checks and their fixes only: what to change is said, and the person (or a
confirmed ``update`` or ``settings`` call) changes it.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

#: The safeguards an installed node runs with: PLINTH's declared storage rules, KEEL's daily
#: growth check and HERALD to carry its alerts. Reached through the registry, never imported.
_STORAGE_RULES = "data.storage_rules"
_SAFEGUARD_SKILLS = ("node.data_growth", "notifications.alert")


def data_safeguard_checks(ctx: SkillContext | None) -> list[dict[str, Any]]:
    """The install's data safeguards, as doctor checks. A gap is said loudly, with the fix:
    a time-series table without a storage rule, a backup that is never restored, or a daily
    growth check with nobody to alert."""
    reg = getattr(ctx, "registry", None)
    if reg is None or not reg.has(_STORAGE_RULES):
        return []
    out: list[dict[str, Any]] = []
    missing = [s for s in _SAFEGUARD_SKILLS if not reg.has(s)]
    out.append({
        "name": "Data growth check",
        "ok": not missing,
        "status": "KEEL checks table growth daily and alerts through HERALD" if not missing
        else f"not installed: {', '.join(missing)}",
        "fix": None if not missing else "enable the node and notifications extensions",
    })
    try:
        from axiom.infra.skill_dispatch import invoke_capability

        res = invoke_capability(reg, _STORAGE_RULES, {}, ctx,
                                surface=getattr(ctx, "surface", None) or "cli")
        v = res.value or {}
        gaps = list(v.get("storage_gaps", v.get("gaps")) or []) if res.ok else list(res.errors)
        backup_gaps = list(v.get("backup_gaps") or []) if res.ok else []
    except Exception as exc:  # noqa: BLE001 - a check that raises is itself a finding
        gaps, backup_gaps = [f"the storage rules could not be read: {exc}"], []
    out.append({
        "name": "Data storage rules",
        "ok": not gaps,
        "refuses_install": bool(gaps),
        "gaps": gaps,
        "status": "every declared table has a storage rule"
        if not gaps else "REFUSED AT INSTALL: " + "; ".join(gaps),
        "fix": None if not gaps else (
            "declare compress_after or retain in data_platform.timeseries, or max_rows, retain "
            "or grows = false in data_platform.table_limits (or install with "
            f"{OVERRIDE_PARAM}='<why>', which is recorded)"
        ),
    })
    out.append({
        "name": "Backup restore check",
        "ok": not backup_gaps,
        "status": "the nightly backup check restores the dump into a scratch database"
        if not backup_gaps else "WARNING: " + "; ".join(backup_gaps),
        "fix": None if not backup_gaps else "set scratch_db in the backup policy",
    })
    return out


#: The explicit override for installing with a time-series table that declares no storage
#: rule. Its value is the reason, and every use is recorded.
OVERRIDE_PARAM = "allow_undeclared_storage"
OVERRIDES_FILE = "install/storage_rule_overrides.jsonl"


def storage_refusal(params: dict[str, Any], ctx: SkillContext | None,
                    checks: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any] | None]:
    """``(refusal, override_record)``: refuse an install while a storage gap stands, unless the
    caller gave the override with a reason; a used override is appended to the record."""
    import json
    import time
    from pathlib import Path

    gaps = [g for c in checks if c.get("refuses_install") for g in c.get("gaps", [])]
    if not gaps:
        return None, None
    reason = str(params.get(OVERRIDE_PARAM) or "").strip()
    if not reason:
        return ("refused: a time-series table has no storage rule: " + "; ".join(gaps)
                + f". Declare one, or pass {OVERRIDE_PARAM}='<why>' (recorded)."), None
    record = {"at": time.time(), "reason": reason, "gaps": gaps,
              "by": str(getattr(ctx, "actor", "") or "")}
    state = getattr(ctx, "state_dir", None)
    if state:
        p = Path(state) / OVERRIDES_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    return None, record


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from axiom.axiom_cli import _gather_diagnostics

    try:
        checks = [dict(c) for c in _gather_diagnostics().get("checks", [])]
    except Exception as exc:  # noqa: BLE001 - a health check that raises is itself a finding
        return SkillResult(
            ok=False, errors=[f"the checks could not run: {type(exc).__name__}: {exc}"]
        )

    try:
        from .binding import report

        binding = report(probe=True)
        checks.append(
            {
                "name": "Site connection",
                "ok": binding["mode"] != "bound" or bool(binding["reachable"]),
                "status": binding["text"],
                "fix": binding.get("fix") or None,
            }
        )
    except Exception:  # noqa: BLE001 - the connection line is added, never required
        pass

    checks.extend(data_safeguard_checks(ctx))

    issues = [c for c in checks if not c.get("ok")]
    lines = [
        f"{'ok  ' if c.get('ok') else 'FIX '} {c['name']}: {c.get('status', '')}" for c in checks
    ]
    for c in issues:
        if c.get("fix"):
            lines.append(f"  fix for {c['name']}: {c['fix']}")
    summary = (
        "Everything checked is healthy."
        if not issues
        else f"{len(issues)} thing(s) need attention."
    )
    value: dict[str, Any] = {
        "healthy": not issues,
        "issues": len(issues),
        "checks": checks,
        "text": "\n".join([summary, *lines]),
    }
    if params.get("error"):
        value["error_reported"] = str(params["error"])
    return SkillResult(ok=True, value=value, actions_taken=["ran the environment checks"])


__all__ = ["run"]
