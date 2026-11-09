# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``memory.forget`` skill — redact recorded fragments from recall.

Resolves the target fragment ids (explicit ids, or a principal's history
optionally narrowed by a content substring) and hands them to
:meth:`CompositionService.forget`, which tombstones each row it is
authorized to (``Right.CONTROL``) and leaves an audit trail.

Two safety rails live here, above the service:

- **No blind principal-wipe.** Forgetting a principal's whole history
  requires an explicit ``all=True``; otherwise a ``match`` substring must
  narrow it. This prevents ``forget --principal X`` from silently erasing
  everything.
- **Dry-run.** ``dry_run=True`` returns exactly what *would* be forgotten
  without touching the store.

The ``composition`` is injected via ``params`` so the skill stays a pure,
testable function; the CLI builds the runtime composition and passes it in.
"""

from __future__ import annotations

import json
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

_AGENT = "axi-memory"


def _ids_for_principal(
    composition: Any, principal: str, match: str | None
) -> list[str]:
    """Fragment ids owned by ``principal``, optionally filtered by a
    case-insensitive substring match against the fragment ``content``.

    Scans every fragment kind (not just EPISODIC) so ``forget`` can reach
    fragments that never surface in ``memory show``.
    """
    ids: list[str] = []
    seen: set[str] = set()
    needle = match.lower() if match else None
    for art in composition.artifact_registry.list(kind="fragment"):
        data = art.data or {}
        prov = data.get("provenance") or {}
        if prov.get("principal_id") != principal:
            continue
        if needle is not None:
            blob = json.dumps(data.get("content") or {}, default=str).lower()
            if needle not in blob:
                continue
        fid = art.name
        if fid not in seen:
            seen.add(fid)
            ids.append(fid)
    return ids


def forget(params: dict[str, Any], ctx: SkillContext | None) -> SkillResult:
    """Redact fragments from recall. See module docstring for the contract."""
    composition = params.get("composition")
    if composition is None:
        return SkillResult(ok=False, errors=["no composition service provided"])

    fragment_ids = list(params.get("fragment_ids") or [])
    principal = params.get("principal")
    match = params.get("match")
    forget_all = bool(params.get("all", False))
    reason = params.get("reason") or "forget"
    dry_run = bool(params.get("dry_run", False))

    # The requester is caller-asserted (consistent with `memory show` /
    # `record`): whoever you say you are is who the CONTROL check runs against.
    # When cryptographic principal auth lands, ctx.principal supersedes this.
    requester = params.get("requester") or principal
    if not requester:
        return SkillResult(
            ok=False,
            errors=["--principal is required (the caller-asserted requester)"],
        )

    # Resolve the target ids.
    if fragment_ids:
        target_ids = fragment_ids
    elif principal:
        target_ids = _ids_for_principal(composition, principal, match)
        if not target_ids:
            return SkillResult(
                ok=True,
                value={"forgotten": [], "denied": [], "not_found": [],
                       "count": 0, "note": "no matching fragments"},
            )
        if not match and not forget_all:
            return SkillResult(
                ok=False,
                errors=[
                    "refusing to forget ALL of "
                    f"{principal}'s fragments — pass --match <text> to narrow, "
                    "or --all to confirm the full purge"
                ],
            )
    else:
        return SkillResult(
            ok=False,
            errors=["specify fragment id(s), or --principal with --match/--all"],
        )

    if dry_run:
        return SkillResult(
            ok=True,
            value={"would_forget": target_ids, "count": len(target_ids),
                   "dry_run": True},
            actions_taken=[],
        )

    result = composition.forget(
        target_ids, requester=requester, agent=_AGENT, reason=reason
    )
    actions = (
        [f"forgot {len(result.forgotten)} fragment(s)"]
        if result.forgotten else []
    )
    return SkillResult(
        ok=not result.denied,
        value={
            "forgotten": result.forgotten,
            "denied": result.denied,
            "not_found": result.not_found,
            "count": result.count,
            "reason": reason,
        },
        errors=(
            [f"{len(result.denied)} fragment(s) denied (requester lacks CONTROL)"]
            if result.denied else []
        ),
        actions_taken=actions,
    )
