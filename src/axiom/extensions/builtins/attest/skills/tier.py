# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tiered gold (unit LD-7, ADR-251 D7).

- ``attest.tier_move`` (CLI only): move one log's year to the cold tier through the switch runner
  (prepare, prove, commit), for a person who may close out the log. Refused, with nothing moved,
  inside the hot window, without a verified archive of every period in it, or when a probe fails.
  ``rollback = true`` moves a committed year back.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillResult

from .. import tiers
from ..service import AttestRefused
from ._common import fail, resolve_site
from .archive import _may_close, _who


def move(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    site, err = resolve_site(params)
    if err:
        return fail(err)
    logbook, who = params.get("logbook"), _who(ctx)
    if not logbook:
        return fail("logbook is required")
    try:
        year = int(params.get("year"))
    except (TypeError, ValueError):
        return fail("year is required, as a number (the calendar year to move)")
    if not who:
        return fail("no person to move as")
    why = _may_close(who, site, logbook, ctx)
    if why:
        return fail(why)
    try:
        if params.get("rollback"):
            have = tiers.get_for(site, logbook, year)
            if have is None:
                return fail(f"{logbook}'s {year} has no move to roll back")
            tiers.plan(site, logbook, year, owner=who).rollback(params.get("reason") or "operator request")
            done = tiers.get(have["move_id"])
            return SkillResult(ok=True, value=done, actions_taken=[f"{logbook}'s {year} is read from the hot tier again"])
        done = tiers.move(site, logbook, year, owner=who, now=tiers._now())  # noqa: SLF001 - the module's clock
    except AttestRefused as exc:
        return fail(str(exc))
    except Exception as exc:  # noqa: BLE001 - the runner's refusal (gate, budget, probe) says why
        return fail(f"{type(exc).__name__}: {exc}")
    return SkillResult(ok=True, value=done, actions_taken=[
        f"moved #{done['from_seq']} to #{done['to_seq']} of {logbook} ({year}) to the cold tier; every read is "
        "unchanged. The soak closes the rollback window."])


__all__ = ["move"]
