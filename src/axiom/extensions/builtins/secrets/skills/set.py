# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.set`` — store a foreign credential (issue #667).

Value ingestion is interactive-prompt or stdin only — the CLI layer
never accepts a value via argv (shell history is one of the plaintext
surfaces this extension exists to close). The skill returns metadata
only; the value goes straight into the custody backend.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..foreign.store import ForeignCredentialStore


def _store_from(params: dict, ctx: SkillContext) -> ForeignCredentialStore:
    injected = params.get("_store")
    if injected is not None:
        return injected
    return ForeignCredentialStore(ctx.state_dir)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = params.get("name")
    if not name:
        return SkillResult(ok=False, errors=["set needs a credential name"])

    raw = params.get("value")
    if not raw and ctx.user_prompt is not None:
        raw = ctx.user_prompt(
            f"Paste the value for {name!r} (stored in the OS keychain; "
            "never echoed to logs): "
        )
    if not raw:
        return SkillResult(ok=False, errors=[
            "no value provided: pipe it on stdin, or run interactively to "
            "be prompted (values are never accepted as CLI arguments)",
        ])
    value = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)

    meta_fields = {
        k: params[k]
        for k in ("provider", "issuer_url", "expires_at", "git_host",
                  "git_username", "notes", "client_id", "host")
        if params.get(k) is not None
    }
    if params.get("auto_rotate") and params.get("no_auto_rotate"):
        return SkillResult(ok=False, errors=["pass either --auto-rotate or --no-auto-rotate, not both"])
    if params.get("auto_rotate"):
        meta_fields["auto_rotate"] = "true"
    elif params.get("no_auto_rotate"):
        meta_fields["auto_rotate"] = "false"
    try:
        store = _store_from(params, ctx)
        meta = store.set(name, value, **meta_fields)
    except (ValueError, KeyError, RuntimeError) as exc:
        return SkillResult(ok=False, errors=[f"set failed: {exc}"])

    from ..foreign import backup_hook

    backup_note = backup_hook.hint(backup_hook.after_change(ctx.state_dir, f"vault set {name}"))
    return SkillResult(
        ok=True,
        value={"metadata": meta},
        actions_taken=[
            f"stored credential {name!r} (value in custody backend)",
            *backup_note,
            *(
                []
                if str(params.get("expires_at") or "").strip()
                else [
                    "WARNING: no expiry recorded, so the expiry audit, the "
                    "rotation pass and the health heartbeat are all blind to "
                    "this credential — permanently, since nothing later "
                    "supplies the field. If the issuer knows the date, "
                    "`axi vault reconcile --apply` will fetch and record it."
                ]
            ),
        ],
    )


__all__ = ["run"]
