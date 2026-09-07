# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``vault.renew`` — KEEP's expiry-rotation policy pass.

``secrets.audit`` already reports which credentials are expired or expiring; its
docstring names this skill as the follow-up that acts on those findings. This is
that follow-up.

KEEP owns *policy*, ``secrets`` owns *mechanism* (adr-001, and the same split
``sweep.py`` documents). Concretely, KEEP decides:

**What "in time" means.** A fixed cadence cannot promise anything on its own —
an issuer that mints week-long tokens will outrun a monthly sweep every time.
What makes the promise keepable is the *ratio*: the horizon must be several
heartbeats wide, so a credential is seen as due many times before it lapses and
a single failed pass is not fatal. At KEEP's hourly heartbeat a 3-day horizon
gives ~72 attempts.

**What may rotate itself.** Only credentials passing both of
``secrets.renewable``'s gates: an unattended provider *and* an explicit operator
opt-in. Everything else escalates to a human. The second gate exists because the
known failure is a human one — rotating a credential whose consumers hold copies
breaks them silently.

**That expiry is always stated.** Every rotation passes an explicit
``expires_at``. Issuer defaults are the trap that created the problem this skill
exists to solve: GitLab's self-rotate API defaults to **seven days**, so a
"successful" unattended rotation with no expiry argument quietly re-arms the
same emergency a week later. Rotating without stating an expiry is how you
rotate forever.

**That silence is never success.** Anything needing a human makes the pass fail,
so KEEP's heartbeat goes visibly red rather than logging a line nobody reads —
the same doctrine as ``sweep``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

# Default horizon. Wide relative to KEEP's hourly heartbeat (~72 passes) and
# wider than the shortest issuer default we have seen in the wild (7 days), so a
# credential is never first noticed on the day it dies.
DEFAULT_HORIZON_DAYS = 3

# Expiry requested for unattended replacements. Long enough that rotation is a
# quarterly event rather than a standing emergency; short enough to stay a
# meaningful control. Overridable per call.
DEFAULT_RENEWED_LIFETIME_DAYS = 90

_ACTIONABLE = ("expired", "expiring")


def _renew_expiry(now: datetime, days: int) -> str:
    return (now + timedelta(days=days)).date().isoformat()


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Audit expiry, rotate what is safe to rotate, escalate the rest."""
    from axiom.extensions.builtins.secrets.skills import audit as audit_skill
    from axiom.extensions.builtins.secrets.skills import renewable as renewable_skill

    horizon = int(params.get("horizon_days", DEFAULT_HORIZON_DAYS))
    lifetime = int(params.get("renewed_lifetime_days", DEFAULT_RENEWED_LIFETIME_DAYS))
    dry_run = bool(params.get("dry_run", False))
    now = params.get("_now") or datetime.now(UTC)

    audit = audit_skill.run({**params, "within_days": horizon}, ctx)
    if not audit.ok:
        return SkillResult(ok=False, errors=list(audit.errors) or ["expiry audit failed"])

    findings = (audit.value or {}).get("findings", [])
    due = [f for f in findings if f.get("level") in _ACTIONABLE]

    actions: list[str] = [
        f"audited {len(findings)} credential(s) against a {horizon}-day horizon"
    ]
    errors: list[str] = []
    rotated: list[str] = []
    escalated: list[dict] = []

    if not due:
        return SkillResult(ok=True, value={
            "horizon_days": horizon, "due": 0, "rotated": [], "escalated": [],
        }, actions_taken=actions + ["nothing due"])

    renew_info = renewable_skill.run(params, ctx)
    by_name = {
        r["name"]: r for r in (renew_info.value or {}).get("credentials", [])
    }

    for finding in due:
        name = finding["name"]
        facts = by_name.get(name, {"auto_renewable": False, "reason": "unknown to the store"})
        level = finding["level"]

        if not facts.get("auto_renewable"):
            escalated.append({
                "name": name, "level": level,
                "expires_at": finding.get("expires_at"),
                "reason": facts.get("reason", ""),
            })
            continue

        if dry_run:
            actions.append(f"would rotate {name} (expires {finding.get('expires_at')})")
            rotated.append(name)
            continue

        try:
            from axiom.extensions.builtins.secrets.skills.rotate_foreign import (
                rotate_foreign,
            )

            outcome = rotate_foreign(
                name,
                {
                    **params,
                    # Always state the expiry — never inherit the issuer default.
                    "expires_at": _renew_expiry(now, lifetime),
                    "surface": "keep-heartbeat",
                },
                ctx,
            )
        except Exception as exc:  # noqa: BLE001
            outcome = SkillResult(ok=False, errors=[f"{exc}"])

        if outcome.ok:
            rotated.append(name)
            actions.append(
                f"rotated {name} unattended; new expiry "
                f"{_renew_expiry(now, lifetime)}"
            )
        else:
            # A failed unattended rotation is an escalation, not a silent retry:
            # the credential is still expiring and now we know automation cannot
            # save it.
            errors.append(
                f"{name}: unattended rotation FAILED — "
                + "; ".join(outcome.errors or ["no detail"])
            )
            escalated.append({
                "name": name, "level": level,
                "expires_at": finding.get("expires_at"),
                "reason": "unattended rotation failed; needs a human now",
            })

    for esc in escalated:
        errors.append(
            f"{esc['name']}: {esc['level']} ({esc['expires_at']}) — "
            f"{esc['reason']}. Rotate with "
            f"`axi secrets rotate {esc['name']} --expires-at <YYYY-MM-DD>`"
        )

    return SkillResult(
        # Escalations make the heartbeat red. A credential a human must touch is
        # exactly the condition that should page.
        ok=not escalated,
        value={
            "horizon_days": horizon,
            "due": len(due),
            "rotated": rotated,
            "escalated": escalated,
        },
        errors=errors,
        actions_taken=actions,
    )


__all__ = ["run", "DEFAULT_HORIZON_DAYS", "DEFAULT_RENEWED_LIFETIME_DAYS"]
