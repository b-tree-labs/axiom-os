# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``vault.audit`` — read-only lifecycle audit across KEEP's two backends.

Issue #667 frames one lifecycle surface over two credential planes:

- **Foreign-minted secrets** (GitLab PATs, webhook URLs, HMAC keys) —
  audited live from the secrets extension's metadata index: expiry
  findings, counts, no keychain access, no values.
- **Axiom-minted capabilities** (ADR-055 capability tokens) — requires
  the vault DB (ADR-052 ``session_for("vault")``). When that DB is not
  wired on this node the section reports ``available: False`` with a
  reason instead of failing the whole audit.

This module is the deterministic core behind the ``axiom_vault_audit``
MCP tool and the (follow-up) ``axi vault audit`` verb.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


def _foreign_section(
    state_dir: Path, *, within_days: int, now: datetime | None,
) -> dict:
    from axiom.extensions.builtins.secrets.foreign.store import (
        ForeignCredentialStore,
    )
    from axiom.extensions.builtins.secrets.skills.audit import audit_findings

    try:
        store = ForeignCredentialStore(state_dir)
        findings = audit_findings(store, within_days=within_days, now=now)
    except Exception as exc:  # noqa: BLE001 — audit degrades, never crashes
        return {"error": f"{type(exc).__name__}: {exc}", "findings": [],
                "counts": {}}
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["level"]] = counts.get(f["level"], 0) + 1
    return {"findings": findings, "counts": counts,
            "within_days": within_days}


def _capability_section() -> dict:
    """Capability-token audit — honest unavailability until the vault DB
    query lands (prd-axiom-vault §5.2 follow-up)."""
    return {
        "available": False,
        "reason": (
            "capability-token audit needs the vault DB (ADR-052 "
            "session_for('vault')); the query surface ships with the "
            "`axi vault audit` verb follow-up"
        ),
    }


def audit_payload(
    *,
    state_dir: Path | None = None,
    within_days: int = 14,
    now: str | datetime | None = None,
) -> dict:
    """JSON-able audit payload. Metadata only — never secret values."""
    if state_dir is None:
        from axiom.infra.paths import get_user_state_dir

        state_dir = get_user_state_dir()
    when: datetime | None
    if isinstance(now, str):
        when = datetime.fromisoformat(now)
    else:
        when = now
    return {
        "foreign_secrets": _foreign_section(
            Path(state_dir), within_days=within_days, now=when,
        ),
        "capabilities": _capability_section(),
    }


__all__ = ["audit_payload"]
