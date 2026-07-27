# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.exposed`` — the leaked-credential closer (ADR-095).

`axi secrets exposed <ref> --where transcript [--detail "…"] [--strategy K] [--value V]`

A credential that appears on any observable surface — a session transcript, a
pasted log, a chat message, a URL — is leaked, full stop. Assessing "how bad"
costs more than rotating. This skill encodes the response as one verb:

1. **Record the exposure** as an append-only event
   (``<state>/secrets/exposures.jsonl``) *before* attempting anything else —
   the exposure is a fact even if rotation then fails.
2. **Force-rotate** the ref by delegating to ``secrets.rotate`` with
   ``force=True`` (exposure always overrides cadence; a caller-supplied
   ``force=False`` is ignored).
3. **Record the rotation outcome** as a second event, linked by ref, so the
   audit trail answers "was that leak closed, and when?" without spelunking.

Like ``rotate``, this skill NEVER prints a secret value — only refs, surfaces,
and outcomes. If rotation fails the skill fails loudly with the exposure
already on record; retry with ``axi secrets exposed`` (idempotent: another
exposure event is harmless) or ``axi secrets rotate --force`` once the backend
issue is fixed.
"""

from __future__ import annotations

import time
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult
from axiom.infra.state import locked_append_jsonl

from ..providers.protocol import SecretRef
from . import rotate as rotate_skill


def _exposure_log(ctx: SkillContext):
    return ctx.state_dir / "secrets" / "exposures.jsonl"


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    raw_ref = params.get("ref")
    if not raw_ref:
        return SkillResult(
            ok=False, errors=["exposed needs a SecretRef (e.g. openbao://kv/data/x)"]
        )
    where = (params.get("where") or "").strip()
    if not where:
        return SkillResult(
            ok=False,
            errors=[
                "exposed needs --where — the surface the credential appeared on "
                "(transcript, log, chat, url, …)"
            ],
        )
    try:
        ref = SecretRef.parse(raw_ref)
    except ValueError as exc:
        return SkillResult(ok=False, errors=[f"bad ref {raw_ref!r}: {exc}"])

    log = _exposure_log(ctx)
    log.parent.mkdir(parents=True, exist_ok=True)

    exposure = {
        "event": "exposure",
        "ts": time.time(),
        "ref": str(ref),
        "where": where,
        "detail": params.get("detail"),
    }
    locked_append_jsonl(log, exposure)  # the fact lands before rotation is attempted
    actions = [f"recorded exposure of {ref} on {where!r} → {log}"]

    rotate_params = dict(params)
    rotate_params["force"] = True  # exposure overrides cadence, always
    rotate_params.pop("where", None)
    rotate_params.pop("detail", None)
    outcome = rotate_skill.run(rotate_params, ctx)

    rotation_event = {
        "event": "rotation",
        "ts": time.time(),
        "ref": str(ref),
        "ok": outcome.ok,
        "new_version": (outcome.value or {}).get("new_version") if outcome.ok else None,
        "errors": outcome.errors or None,
    }
    locked_append_jsonl(log, rotation_event)

    if not outcome.ok:
        return SkillResult(
            ok=False,
            errors=[
                *outcome.errors,
                "exposure is on record; rotation failed and must be retried "
                "(axi secrets rotate --force once the backend issue is fixed)",
            ],
            actions_taken=actions + list(outcome.actions_taken or []),
            value={"exposure": exposure, "rotation": None},
        )

    return SkillResult(
        ok=True,
        value={"exposure": exposure, "rotation": outcome.value},
        actions_taken=actions + list(outcome.actions_taken or []),
    )
