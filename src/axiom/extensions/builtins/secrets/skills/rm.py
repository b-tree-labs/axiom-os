# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.rm`` — delete a foreign credential.

Deletion destroys the only copy of the value, so it is human-gated:
``--yes`` or an exact-name interactive confirmation. Every removal is
journaled to the #665 action ledger (metadata only) so "where did that
credential go?" always has an answer.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..foreign.store import ForeignCredentialStore


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = params.get("name")
    if not name:
        return SkillResult(ok=False, errors=["rm needs a credential name"])
    store = params.get("_store") or ForeignCredentialStore(ctx.state_dir)

    if not store.exists(name):
        return SkillResult(
            ok=False, errors=[f"no foreign credential named {name!r}"],
        )

    if not params.get("yes"):
        if ctx.user_prompt is None:
            return SkillResult(ok=False, errors=[
                f"refusing to delete {name!r} headless without --yes: "
                "deletion destroys the only copy of the value",
            ])
        typed = ctx.user_prompt(
            f"Deleting {name!r} destroys the stored value. "
            f"Type the name to confirm: "
        )
        if typed != name:
            return SkillResult(
                ok=False, errors=["confirmation did not match; not deleted"],
            )

    try:
        store.remove(name)
    except (KeyError, RuntimeError) as exc:
        return SkillResult(ok=False, errors=[f"rm failed: {exc}"])

    action_id = None
    try:
        from axiom.policy.action_ledger import ActionLedger

        record = ActionLedger(state_dir=ctx.state_dir).record_action(
            agent="keep",
            op_class="secrets.rm",
            name="delete_foreign_credential",
            candidate=name,
            guards=["human_confirmation"],
            outcome="proceeded",
            metadata={"surface": "cli"},
        )
        action_id = record.get("id")
    except Exception:  # noqa: BLE001 — journaling never blocks the delete report
        ctx.logger.debug("action-ledger emission failed for secrets.rm",
                         exc_info=True)

    return SkillResult(
        ok=True,
        value={"removed": name, "action_id": action_id},
        actions_taken=[f"deleted credential {name!r} (value + metadata)"],
    )


__all__ = ["run"]
