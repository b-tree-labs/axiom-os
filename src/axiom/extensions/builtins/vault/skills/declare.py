# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Record what can be done about a credential, so its finding can be cleared.

The audit now asks for a disposition rather than an expiry that may not exist.
That is only an improvement if answering is easy — a finding whose remedy takes
ten minutes of research stays unactioned exactly as long as one whose remedy was
wrong.

Refuses an unknown disposition rather than storing it: a typo that silently
persisted would let somebody believe they had classified a credential when they
had not, and the finding would vanish without the question being answered.

Refuses ``externally_owned`` without an owner for the same reason. Recording
"somebody else's" without saying whose leaves a finding nobody can act on, which
is the state this exists to end.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult, ensure_context


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """``axi vault declare <name> --disposition <d> [--review-by ...]``."""
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
    from axiom.extensions.builtins.vault.disposition import DISPOSITIONS, classify

    ctx = ensure_context(ctx)
    store = params.get("_store") or ForeignCredentialStore(ctx.state_dir)

    name = str(params.get("name") or "").strip()
    disposition = str(params.get("disposition") or "").strip()
    if not name:
        return SkillResult(ok=False, errors=["name is required"])
    if disposition not in DISPOSITIONS:
        return SkillResult(ok=False, errors=[
            f"unknown disposition {disposition!r}; expected one of "
            + ", ".join(DISPOSITIONS)
        ])
    if not store.exists(name):
        return SkillResult(ok=False, errors=[
            f"no credential named {name!r} in the store"
        ])

    owner = str(params.get("owner") or "").strip()
    if disposition == "externally_owned" and not owner:
        return SkillResult(ok=False, errors=[
            f"{name} is externally owned, so record the owner to contact "
            f"(--owner): without one the finding is unactionable"
        ])

    updates: dict[str, str | None] = {"disposition": disposition}
    for field in ("review_by", "owner"):
        value = str(params.get(field) or "").strip()
        if value:
            updates[field] = value
    store.update_metadata(name, **updates)

    # Declaring is a step, not always the finish. Reporting success while the
    # next heartbeat repeats the same red finding is how a remedy loses trust.
    verdict = classify({**store.metadata(name), "name": name})
    actions = [f"{name}: disposition = {disposition}"]
    if verdict["status"] != "ok":
        actions.append(f"still outstanding — {verdict['remedy']}")

    return SkillResult(ok=True, value={"name": name, "disposition": disposition,
                                       "remaining": verdict["status"]},
                       actions_taken=actions)


__all__ = ["run"]
