# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``synthetic.smoke``: run one role's smoke journeys against an address (LOOP-S5, LOOP-S6).

``ok`` means the skill ran; the verdict is ``value["status"]``. With ``commit`` the
verdict is also kept in the journey store, where the release cut reads it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import store
from ..loader import JourneyError
from ..smoke import run_smoke, smoke_set


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    manifest = str(params.get("manifest") or "").strip()
    role = str(params.get("role") or "").strip()
    base_url = str(params.get("base_url") or "").strip()
    if not (manifest and role and base_url):
        return SkillResult(ok=False, value=None, errors=["give manifest, role and base_url"])
    try:
        sset = smoke_set(manifest, role)
    except JourneyError as exc:
        return SkillResult(ok=False, value=None, errors=[str(exc)])
    result = run_smoke(sset, base_url, env=params.get("env"))
    actions = [f"ran {len(sset.journeys)} smoke journey(s) for {role} against {base_url}: {result['status']}"]
    commit = str(params.get("commit") or "").strip()
    if commit:
        state_dir = Path(params.get("state_dir") or (ctx.state_dir if ctx is not None else "."))
        try:
            result["recorded"] = store.record(state_dir, commit=commit, result=result,
                                              source=str(params.get("source") or "local"))
        except ValueError as exc:
            return SkillResult(ok=False, value=result, errors=[str(exc)])
        actions.append(f"recorded the verdict for {commit[:12]} in the journey store")
    return SkillResult(ok=True, value=result, actions_taken=actions)


__all__ = ["run"]
