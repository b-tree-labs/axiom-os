# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``video.revoke``: stop a source, release it, and remove its declaration and records.

The last step of the connector lifecycle (ADR-238 D5). The login is left where it
is: the vault owns it and another source may use it, so the result says it is
still stored and how to remove it.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..connector import load, revoke
from ._common import cli, need_name, source_name, state_dir

FOREIGN = "foreign://"


def _login_note(ref: str | None) -> str:
    if not ref:
        return ""
    if ref.startswith(FOREIGN):
        entry = ref[len(FOREIGN) :]
        return f"its login is still in the vault as {entry}: `{cli()} vault rm {entry}` removes it, if nothing else uses it"
    return f"its login stays where it is ({ref})"


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = source_name(params)
    if not name:
        return need_name("revoke")
    sd = state_dir(ctx)
    ref = load(name, state_dir=sd).credential_ref  # declared: the precondition says so
    outcome = revoke(name, state_dir=sd)
    note = _login_note(ref)
    action = f"revoked {name}" + (", after stopping its capture" if outcome["was_running"] else "")
    return SkillResult(
        value={**outcome, "credential_ref": ref, "note": note},
        actions_taken=[action, *([note] if note else [])],
    )


__all__ = ["run"]
