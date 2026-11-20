# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The expiry half of ``vault.audit`` — findings over credential metadata.

Pure metadata pass: expired / expiring (within ``within_days``, default
14) / ok / no_expiry, per credential. This is the deterministic surface
KEEP's future proactive-rotation scheduling reads (follow-up issue);
here it only reports. Values are never touched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..foreign.store import ForeignCredentialStore

DEFAULT_WITHIN_DAYS = 14


def _parse_when(raw: str) -> datetime | None:
    from axiom.extensions.builtins.vault.index import parse_when

    return parse_when(raw)


def _cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def audit_findings(
    store: ForeignCredentialStore,
    *,
    within_days: int = DEFAULT_WITHIN_DAYS,
    now: datetime | None = None,
) -> list[dict]:
    """One finding per credential — the shared core for CLI + MCP."""
    from axiom.extensions.builtins.vault.index import CredentialIndex

    # The same health `vault list` and `vault resolve` show (ADR-218).
    rows = CredentialIndex(store=store, now=now or datetime.now(UTC), within_days=within_days).rows()
    details = {
        "no_expiry": "no expiry recorded; set one so KEEP can audit it",
        "unparseable": "expires_at is not ISO-8601; fix the metadata",
        "expired": "credential expiry has passed — rotate now",
        "ok": "expiry beyond the audit horizon",
    }
    findings: list[dict] = []
    for row in rows:
        name, level = row["name"], row["health"]
        detail = details.get(level) or (
            f"expires within {within_days} days — rotate with `{_cli()} vault rotate {name}`"
        )
        findings.append({
            "name": name, "level": level, "expires_at": row.get("expires_at") or None,
            "detail": detail,
        })
    return findings


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    store = params.get("_store") or ForeignCredentialStore(ctx.state_dir)
    within = int(params.get("within_days", DEFAULT_WITHIN_DAYS))
    now = None
    if params.get("_now"):
        now = _parse_when(str(params["_now"]))
    try:
        findings = audit_findings(store, within_days=within, now=now)
    except Exception as exc:  # noqa: BLE001
        return SkillResult(ok=False, errors=[f"audit failed: {exc}"])

    counts: dict[str, int] = {}
    for f in findings:
        counts[f["level"]] = counts.get(f["level"], 0) + 1
    return SkillResult(
        ok=True,
        value={
            "within_days": within,
            "findings": findings,
            "counts": counts,
        },
    )


__all__ = ["run", "audit_findings", "DEFAULT_WITHIN_DAYS"]
