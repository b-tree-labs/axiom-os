# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.get`` — metadata by default; value only on explicit ``--reveal``.

The reveal path exists for operator break-glass (pasting into a vendor
console). It is CLI-only — the MCP surface never exposes this skill —
and the result carries an explicit warning string the CLI must print.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..foreign.store import ForeignCredentialStore

REVEAL_WARNING = (
    "WARNING: credential value revealed on this terminal. Anything that "
    "captures this screen/session (scrollback, transcript, screen share) "
    "now holds it — rotate if in doubt."
)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = params.get("name")
    if not name:
        return SkillResult(ok=False, errors=["get needs a credential name"])
    store = params.get("_store") or ForeignCredentialStore(ctx.state_dir)

    try:
        meta = store.metadata(name)
    except KeyError as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    if not params.get("reveal"):
        return SkillResult(ok=True, value={"metadata": meta})

    try:
        with store.get(name) as secret:
            value = secret.as_str()
    except (KeyError, RuntimeError) as exc:
        return SkillResult(ok=False, errors=[f"get failed: {exc}"])
    return SkillResult(
        ok=True,
        value={"metadata": meta, "value": value, "warning": REVEAL_WARNING},
    )


__all__ = ["run"]
