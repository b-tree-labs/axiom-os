# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``instrument.list``: every instrument of every kind, or the registered kinds. Reads only."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ._common import did_not_answer


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from .. import registry

    cat = registry.catalog()
    if params.get("kinds"):
        return SkillResult(value={"kinds": cat.rows()})
    instruments: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    kinds = cat.kinds()
    for kind in kinds:
        try:
            instruments += [row.as_dict() for row in kind.list()]
        except Exception as exc:  # noqa: BLE001 - one kind not answering is that kind's problem
            failed.append(did_not_answer(kind.name, exc))
    return SkillResult(
        value={"instruments": instruments, "failed": failed, "registered": len(kinds)}
    )


__all__ = ["run"]
