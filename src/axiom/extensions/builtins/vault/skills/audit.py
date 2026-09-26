# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""KEEP's credential-health report — metadata only, never a value.

This was a stub that printed "not yet implemented" and **exited 0**. An audit
that reports success when it did not run is worse than one that does not exist:
`axi vault audit` is what an operator or a scheduled job runs to find a
credential about to expire, and every caller was told it passed.

``no_expiry`` is reported as a FINDING, not as healthy. A credential with no
recorded expiry cannot be audited by anything, ever, and the first symptom is a
401 in somebody's face — which is how a live token was lost.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult, ensure_context

#: Levels that mean a human should look. ``no_expiry`` is among them by design.
_FINDINGS = ("expired", "expiring", "no_expiry")


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Expiry health across every recorded credential."""
    from axiom.extensions.builtins.secrets.skills import audit as secrets_audit

    ctx = ensure_context(ctx)
    horizon = int(params.get("within_days", 14))

    inner = secrets_audit.run({**params, "within_days": horizon}, ctx)
    if not inner.ok:
        return SkillResult(ok=False, errors=list(inner.errors) or ["expiry audit failed"])

    findings = list((inner.value or {}).get("findings", []))
    flagged = [f for f in findings if f.get("level") in _FINDINGS]
    by_level: dict[str, list[str]] = {}
    for finding in flagged:
        by_level.setdefault(str(finding.get("level")), []).append(str(finding.get("name")))

    return SkillResult(
        # Findings make this red. A green audit alongside a credential nobody
        # can renew is the state that let one die.
        ok=not flagged,
        value={
            "within_days": horizon,
            "total": len(findings),
            "flagged": len(flagged),
            "by_level": by_level,
            "findings": flagged,
        },
        actions_taken=[f"audited {len(findings)} credential(s) over {horizon} days"],
        errors=[
            f"{f['name']}: {f['level']}"
            + (f" (expires {f['expires_at']})" if f.get("expires_at") else "")
            for f in flagged
        ],
    )


__all__ = ["run"]
