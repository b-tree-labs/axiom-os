# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Foreign-credential rotation flow (issue #667, owner directive).

``axi secrets rotate <name>`` — the KEEP-executed sequence:

1. read the current value from the custody backend,
2. mint a replacement at the issuer via the RotationProvider factory
   (``gitlab-pat`` API rotation; ``guided`` interactive fallback),
3. write the new value back through the store (+ expiry metadata),
4. probe-verify the replacement against the issuer,
5. report scrub candidates (plaintext location types — never auto-edited).

The whole exchange runs inside ``guarded_act`` (agent ``keep``, op class
``secrets.rotate``) so every rotation — refusals and failures included —
journals to the #665 action ledger. Journal records carry metadata and
issuer handles ONLY; secret values stay between this process and the
custody backend.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult
from axiom.policy.agent_action_guard import AgentAction, guarded_act

from ..foreign.rotation_providers import (
    ForeignRotationError,
    build_rotation_provider,
)
from ..foreign.scrub import scrub_candidates
from ..foreign.store import ForeignCredentialStore

AGENT = "keep"
OP_CLASS = "secrets.rotate"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _action_id_for(state_dir, name: str) -> str | None:
    """Most-recent ledger record for this rotation — the journal handle."""
    try:
        from axiom.policy.action_ledger import ActionLedger

        rows = ActionLedger(state_dir=state_dir).query(
            op_class=OP_CLASS, text=name, limit=1,
        )
        return rows[0]["id"] if rows else None
    except Exception:  # noqa: BLE001 — handle lookup never breaks the flow
        return None


def rotate_foreign(
    name: str, params: dict[str, Any], ctx: SkillContext,
) -> SkillResult:
    store: ForeignCredentialStore = (
        params.get("_store") or ForeignCredentialStore(ctx.state_dir)
    )
    try:
        meta = store.metadata(name)
    except KeyError:
        known = ", ".join(m["name"] for m in store.list()) or "(none stored)"
        return SkillResult(ok=False, errors=[
            f"no foreign credential named {name!r}; known: {known}. "
            "(Refs like scheme://path rotate via the backend strategies.)",
        ])

    kind = params.get("provider") or meta.get("provider") or "guided"
    provider = params.get("_rotation_provider")
    if provider is None:
        try:
            provider = build_rotation_provider(
                kind,
                issuer_url=params.get("issuer_url") or meta.get("issuer_url"),
                http=params.get("_http"),
                user_prompt=ctx.user_prompt,
            )
        except KeyError as exc:
            return SkillResult(ok=False, errors=[str(exc)])

    outcome: dict[str, Any] = {}

    def do_one(candidate: str) -> bool:
        try:
            with store.get(candidate) as current_secret:
                current = current_secret.as_str()
            minted = provider.rotate(
                current, expires_at=params.get("expires_at"),
            )
            store.set(
                candidate,
                minted.new_value.encode("utf-8"),
                expires_at=minted.expires_at,
                last_rotated_at=_now_iso(),
            )
            outcome["expires_at"] = minted.expires_at
            outcome["issuer_handle"] = minted.handle
            probe_ok, probe_detail = provider.probe(minted.new_value)
            outcome["probe_ok"] = probe_ok
            outcome["probe_detail"] = probe_detail
            if not probe_ok:
                outcome["error"] = (
                    f"replacement stored but probe-verify failed: "
                    f"{probe_detail} — verify at the issuer before relying "
                    "on it"
                )
                return False
            return True
        except (ForeignRotationError, KeyError, RuntimeError, ValueError) as exc:
            outcome["error"] = f"rotation failed: {exc}"
            return False

    # reversible=True: rotation preserves the holder's access (a fresh
    # credential replaces the old one; recovery is re-minting at the
    # issuer). The consequential part — the old token dying — is exactly
    # what the ledger journal + guard rails are wrapped around.
    action = AgentAction(
        agent=AGENT,
        op_class=OP_CLASS,
        name="rotate_foreign_credential",
        candidates=[name],
        reversible=True,
        metadata={"provider": kind, "surface": params.get("surface", "cli")},
    )
    decision = guarded_act(action, do_one=do_one, state_dir=ctx.state_dir)
    action_id = _action_id_for(ctx.state_dir, name)

    value: dict[str, Any] = {
        "name": name,
        "provider": kind,
        "rotated": name in decision.completed,
        "probe_ok": outcome.get("probe_ok"),
        "probe_detail": outcome.get("probe_detail"),
        "expires_at": outcome.get("expires_at"),
        "issuer_handle": outcome.get("issuer_handle"),
        "action_id": action_id,
        "scrub_candidates": scrub_candidates(name),
    }

    if not decision.proceed:
        return SkillResult(ok=False, value=value, errors=[
            f"rotation refused by the action guard: {decision.reason}",
        ])
    if name not in decision.completed:
        return SkillResult(ok=False, value=value, errors=[
            outcome.get("error", "rotation did not complete"),
        ])
    actions = [
        f"rotated foreign credential {name!r} via provider {kind!r}",
        "probe-verified the replacement against the issuer"
        if outcome.get("probe_ok")
        else "replacement not probe-verified (guided provider without probe)",
        "journaled to the action ledger"
        + (f" (record {action_id})" if action_id else ""),
        "review scrub candidates: superseded value may persist in the "
        "listed plaintext locations (reported, never auto-edited)",
    ]
    return SkillResult(ok=True, value=value, actions_taken=actions)


__all__ = ["rotate_foreign", "AGENT", "OP_CLASS"]
