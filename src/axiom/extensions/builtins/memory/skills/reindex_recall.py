# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``memory.reindex_recall`` skill — backfill the semantic recall corpus
from the ledger (ADR-088 §5, one-time migration).

The recall corpus (``recall.db``) is a rebuildable read-side projection of
the ledger (``artifacts.db``). Any fragment that was recorded while the write
path lacked a ``recall_index`` (the pre-fix append→recall gap) sits in the
ledger but was never projected, so ``recall`` serves nothing for it. This
skill re-projects — for one principal or every principal in the ledger — by
delegating to the idempotent :meth:`RecallIndex.rebuild`, which drops and
rebuilds each principal's corpus from the ledger. Safe to re-run: the
projection is disposable, so a second pass is a no-op net of new fragments.

Vault and other non-projectable cognitive types are skipped by
``index_fragment`` — reindex never widens what recall can serve.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def _distinct_principals(composition: Any) -> list[str]:
    """Every principal that owns at least one live fragment in the ledger."""
    registry = composition.artifact_registry
    backend = getattr(registry, "_backend", None)
    if backend is not None and hasattr(backend, "find_fragments"):
        artifacts = backend.find_fragments()
    else:  # pragma: no cover - registries without the JSON1 fast path
        artifacts = registry.list(kind="fragment")
    principals: set[str] = set()
    for artifact in artifacts:
        pid = (artifact.data or {}).get("provenance", {}).get("principal_id")
        if pid:
            principals.add(pid)
    return sorted(principals)


def reindex_recall(
    params: dict[str, Any], ctx: SkillContext | None
) -> SkillResult:
    """Rebuild the recall projection for one principal or all of them."""
    composition = params.get("composition")
    if composition is None:
        return SkillResult(ok=False, errors=["no composition service provided"])
    recall_index = getattr(composition, "recall_index", None)
    if recall_index is None:
        return SkillResult(
            ok=False,
            errors=[
                "composition has no recall_index; nothing to reindex into "
                "(build it with recall_index=RecallIndex(...))"
            ],
        )

    reindex_all = bool(params.get("all", False))
    if reindex_all:
        principals = _distinct_principals(composition)
    else:
        principal = params.get("principal")
        if not principal:
            return SkillResult(
                ok=False,
                errors=["--principal is required (or pass --all)"],
            )
        principals = [principal]

    per_principal: dict[str, int] = {}
    total = 0
    for principal in principals:
        count = recall_index.rebuild(composition, principal=principal)
        per_principal[principal] = count
        total += count

    actions = []
    if total:
        actions.append(
            f"reindexed {total} fragment(s) across "
            f"{len(principals)} principal(s) into the recall corpus"
        )
    return SkillResult(
        ok=True,
        value={
            "principals": principals,
            "reindexed": total,
            "per_principal": per_principal,
        },
        actions_taken=actions,
    )
