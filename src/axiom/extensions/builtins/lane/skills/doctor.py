# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``lane.doctor`` — the registry against the machine.

Deterministic by itself. With ``explain=True`` and a local reasoning model
available, it also says which finding is the one standing between the caller
and whatever they said they were doing (ADR-139).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import doctor as engine
from .. import explain as reasoning
from ..registry import Registry
from . import lanes_path


def _venvs(root: Path, extra: list[str]) -> list[Path]:
    found = [Path(p) for p in extra]
    for candidate in (root / ".venv", *(root.glob("*/.venv"))):
        if candidate.is_dir():
            found.append(candidate)
    home_venv = Path.home() / ".axi" / "service-venv"
    if home_venv.is_dir():
        found.append(home_venv)
    return list(dict.fromkeys(found))


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    root = Path(params.get("root") or Path.cwd()).resolve()
    workspace = Path(params.get("workspace") or root)
    reg = Registry(params.get("registry") or lanes_path())
    lanes = reg.all()

    findings = engine.run(
        lanes,
        venvs=_venvs(workspace, list(params.get("venvs") or [])),
        root=workspace,
        listening=params.get("listening"),
    )

    commentary = reasoning.explain(
        findings,
        ask=params.get("ask") if params.get("explain") else None,
        caller_goal=str(params.get("caller_goal") or ""),
        context={"lanes": ", ".join(sorted(lanes)) or "none claimed"},
    )

    worst = min(
        (f.level for f in findings),
        key=lambda level: {"broken": 0, "drift": 1, "note": 2}.get(level, 9),
        default="",
    )
    return SkillResult(
        # A checker reports; it does not fail. `ok=False` would make a CI step
        # red for a note, and the notes are the ones people should keep reading.
        value={
            "findings": [
                {"level": f.level, "subject": f.subject, "detail": f.detail, "fix": f.fix}
                for f in findings
            ],
            "worst": worst or None,
            "summary": commentary.text or None,
            "commentary_unavailable": commentary.unavailable or None,
            "text": reasoning.render(findings, commentary),
        }
    )
