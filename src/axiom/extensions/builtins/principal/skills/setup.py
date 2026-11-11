# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``principal.setup`` — run or resume the interview, then prove delivery.

Non-interactive callers pass ``answers`` directly, which is how the MCP tool and
a headless install both use it. When required answers are missing the skill
returns the next question rather than guessing, so an agent session in an IDE can
carry the conversation turn by turn.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..interview import apply_answers, next_question
from ..provision import ProvisionError, provision
from ..store import load, save
from . import verify as verify_skill


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    config_dir = params.get("config_dir")
    answers = params.get("answers") or {}

    # A non-human principal has nobody to interview, so a declaration takes the
    # declarative path (spec §3.3). This is a branch rather than an interview
    # with defaults filled in: a record that looks interviewed and was not is
    # the "configured therefore fine" failure this extension exists to prevent.
    declaration = params.get("declaration")
    if declaration:
        return _provision_declared(declaration, params, ctx, config_dir=config_dir)

    pending = next_question(answers)
    if pending is not None:
        existing = load(config_dir=config_dir)
        return SkillResult(
            ok=True,
            value={
                "complete": False,
                "next_question": {
                    "key": pending.key,
                    "prompt": pending.prompt,
                    "help": pending.help,
                },
                "current_status": existing.status.value if existing else "absent",
            },
            actions_taken=[f"Interview in progress — next: {pending.prompt}"],
        )

    try:
        principal = apply_answers(
            answers,
            handle=params.get("handle") or "",
            directory_ref=params.get("directory_ref"),
        )
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    save(principal, config_dir=config_dir)
    actions = [f"Recorded principal {principal.display_name} ({principal.status.value})"]

    if params.get("skip_verify"):
        actions.append(
            "Skipped the comms test — the harness stays unverified and routing has "
            "nothing it is allowed to use."
        )
        return SkillResult(
            ok=True,
            value={"complete": True, "verified": False, "status": principal.status.value},
            actions_taken=actions,
        )

    result = verify_skill.run({"config_dir": config_dir, "send": params.get("send")}, ctx)
    actions += result.actions_taken
    detail = result.value or {}
    # Keys are named apart deliberately: `verified` is "did anything round-trip",
    # while the verify skill's own `verified` is the list of which kinds did.
    # Merging them blindly makes one field mean two things depending on caller.
    return SkillResult(
        ok=result.ok,
        value={
            "complete": True,
            "verified": result.ok,
            "verified_endpoints": detail.get("verified", []),
            "degraded_endpoints": detail.get("degraded", []),
            "fully_verified": detail.get("fully_verified", False),
            "status": detail.get("status"),
        },
        errors=result.errors,
        actions_taken=actions,
    )


def _provision_declared(
    declaration: dict[str, Any],
    params: dict[str, Any],
    ctx: SkillContext,
    *,
    config_dir: Any,
) -> SkillResult:
    """Declare a non-human principal, then prove its endpoints by round trip."""
    try:
        principal = provision(declaration)
    except ProvisionError as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    save(principal, config_dir=config_dir)
    actions = [
        f"Declared {principal.kind.value} principal {principal.handle} "
        f"({principal.status.value}) — no interview, no style card"
    ]

    if params.get("skip_verify"):
        actions.append(
            "Skipped the comms test — the endpoints are declared, not proven, and "
            "routing has nothing it is allowed to use."
        )
        return SkillResult(
            ok=True,
            value={"complete": True, "verified": False, "status": principal.status.value},
            actions_taken=actions,
        )

    result = verify_skill.run(
        {
            "config_dir": config_dir,
            "send": params.get("send"),
            "roundtrip": params.get("roundtrip"),
        },
        ctx,
    )
    actions += result.actions_taken
    detail = result.value or {}
    return SkillResult(
        ok=result.ok,
        value={
            "complete": True,
            "kind": principal.kind.value,
            "verified": result.ok,
            "verified_endpoints": detail.get("verified", []),
            "degraded_endpoints": detail.get("degraded", []),
            "fully_verified": detail.get("fully_verified", False),
            "status": detail.get("status"),
        },
        errors=result.errors,
        actions_taken=actions,
    )
