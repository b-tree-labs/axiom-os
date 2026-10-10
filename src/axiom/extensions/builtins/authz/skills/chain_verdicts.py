# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``audit.chain`` — walk a verdict's provenance backwards (PRD §5.4).

Starting from one receipt id, follow ``provenance_parent`` until we hit
a root (a verdict with no upstream parent, or a parent id we cannot
resolve). Returns the chain newest-first.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..db_models import Verdict
from .list_verdicts import _resolve_session, _to_row

_MAX_DEPTH = 100  # cycle / runaway guard


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    receipt_id = params.get("receipt_id") or ""
    if not receipt_id:
        return SkillResult(
            ok=False, errors=["receipt_id is required (positional arg)"],
        )

    chain: list[dict[str, Any]] = []
    seen: set[str] = set()

    with _resolve_session(params) as session:
        current_id: str | None = receipt_id
        while current_id and current_id not in seen and len(chain) < _MAX_DEPTH:
            seen.add(current_id)
            v = (
                session.query(Verdict)
                .filter(Verdict.id == current_id)
                .one_or_none()
            )
            if v is None:
                # Walked off the end of the verdict table — the parent is
                # a non-verdict fragment (legitimate root). Record the
                # break so the caller can tell apart "no parent" from
                # "parent fragment id was bogus".
                chain.append({
                    "id": current_id,
                    "kind": "external_root",
                    "note": "no verdict row; provenance origin is a "
                            "non-verdict fragment (e.g. user-prompt or boot).",
                })
                break
            row = _to_row(v)
            row["kind"] = "verdict"
            row["provenance_parent"] = v.provenance_parent
            chain.append(row)
            current_id = v.provenance_parent or None

        truncated = len(chain) >= _MAX_DEPTH

    if not chain:
        return SkillResult(
            ok=False, errors=[f"no verdict found with id={receipt_id!r}"],
        )

    return SkillResult(
        ok=True,
        value={
            "resource": "chain",
            "root_id": receipt_id,
            "depth": len(chain),
            "truncated": truncated,
            "items": chain,
        },
    )
