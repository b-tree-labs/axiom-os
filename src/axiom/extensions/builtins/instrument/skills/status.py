# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``instrument.status``: one summary per kind; a kind that cannot answer says why. Reads only."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..kinds import KindStatus
from ._common import did_not_answer


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from .. import registry

    cat = registry.catalog()
    summaries: list[dict[str, Any]] = []
    for kind in cat.kinds():
        try:
            status = kind.status()
            if not isinstance(status, KindStatus):
                raise TypeError(f"status() returned {type(status).__name__}, not a KindStatus")
            summaries.append(status.as_dict())
        except Exception as exc:  # noqa: BLE001 - one kind not answering is that kind's problem
            failed = did_not_answer(kind.name, exc)
            summaries.append(
                KindStatus(
                    kind=kind.name, ok=False, summary="did not answer", error=failed["error"],
                    fix=failed["fix"],
                ).as_dict()
            )
    # A kind that is declared but cannot be used is part of the node's status too.
    for row in cat.rows():
        if row["state"] != "registered":
            summaries.append(
                KindStatus(
                    kind=row["name"], ok=False, summary=row["state"], error=row["reason"], fix=row["fix"]
                ).as_dict()
            )
    return SkillResult(value={"kinds": sorted(summaries, key=lambda s: s["kind"])})


__all__ = ["run"]
