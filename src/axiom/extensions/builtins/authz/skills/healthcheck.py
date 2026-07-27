# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``audit.healthcheck`` — operator-runnable GUARD readiness probe.

Verifies the production wiring is actually live before extensions
depend on it. Reports:

  - runtime mode (AXIOM_MODE)
  - schema state (verdicts / policies / graduation tables present?)
  - decide() reachability (returns a verdict on a synthetic envelope)

Exits 0 only when every probe passes. CI + post-deploy checks consume
the JSON form (``--json``).
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def _probe_schema() -> dict[str, Any]:
    """Check that the authz schema + tables are present + readable."""
    try:
        from sqlalchemy import text

        from axiom.infra.db import session_for

        result: dict[str, Any] = {"ok": True, "tables": {}}
        with session_for("authz") as session:
            for tbl in ("verdicts", "policies", "graduation"):
                try:
                    row = session.execute(
                        text(f"SELECT count(*) FROM {tbl}")  # noqa: S608
                    ).scalar()
                    result["tables"][tbl] = {"ok": True, "rows": int(row or 0)}
                except Exception as exc:  # noqa: BLE001 - operator surface
                    result["ok"] = False
                    result["tables"][tbl] = {"ok": False, "error": str(exc)[:200]}
        return result
    except Exception as exc:  # noqa: BLE001 - operator surface
        return {"ok": False, "error": str(exc)[:200]}


def _probe_decide() -> dict[str, Any]:
    """Synthesize an envelope, call ``decide``, expect a typed verdict."""
    try:
        from datetime import UTC, datetime, timedelta

        from axiom.extensions.builtins.authz import DecideContext
        from axiom.extensions.builtins.authz.decide import decide
        from axiom.governance.capability import CapabilityToken
        from axiom.governance.classification import Classification
        from axiom.governance.envelope import ActionEnvelope
        from axiom.governance.intent import ActionIntent
        from axiom.governance.principal import Principal
        from axiom.governance.provenance import ProvenanceRef
        from axiom.governance.resource import ResourceRef
        from axiom.infra.db import session_for

        synthetic_actor = Principal(
            handle="@healthcheck:axi-audit",
            public_bytes=b"\x00" * 32,
        )
        synthetic_capability = CapabilityToken(
            subject=synthetic_actor.handle,
            intent_scope="audit.healthcheck",
            resource_scope="probe://synthetic",
            classification_ceiling=Classification.INTERNAL,
            issued_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=1),
            issuer="axi-audit-healthcheck",
            signature=b"healthcheck-synthetic",
        )
        env = ActionEnvelope(
            actor=synthetic_actor,
            capability=synthetic_capability,
            classification=Classification.INTERNAL,
            context_fragment_id="probe://healthcheck",
            provenance_parent=ProvenanceRef.synthetic("healthcheck"),
            federation_origin=None,
            intent=ActionIntent("audit.healthcheck.probe"),
            resource=ResourceRef.parse("probe://synthetic"),
            deadline=None,
            dedup_key="healthcheck-synthetic",
        )
        ctx = DecideContext(session_factory=lambda: session_for("authz"))
        verdict = decide(env, ctx)
        return {
            "ok": True,
            "decision": verdict.decision,
            "next_action": verdict.next_action_for_caller.value
                if hasattr(verdict.next_action_for_caller, "value")
                else str(verdict.next_action_for_caller),
        }
    except Exception as exc:  # noqa: BLE001 - operator surface
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from axiom.governance.mode import current_mode

    mode = current_mode()
    schema = _probe_schema()
    decide_probe = _probe_decide()

    overall_ok = schema.get("ok") and decide_probe.get("ok")
    errors: list[str] = []
    if not schema.get("ok"):
        errors.append(f"schema probe failed: {schema.get('error', schema)}")
    if not decide_probe.get("ok"):
        errors.append(f"decide probe failed: {decide_probe.get('error')}")

    return SkillResult(
        ok=overall_ok,
        value={
            "resource": "healthcheck",
            "mode": mode,
            "schema": schema,
            "decide": decide_probe,
        },
        errors=errors,
    )
