# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data.adopt_tick``: one bounded batch of an unattended adoption copy.

What the orchestrator's PULSE tick fires for a job installed with
``axi data adopt-timeseries <table> --schedule``. It waits (bounded) for the
database, copies windows until its time budget, and returns; the next fire
continues from the ledger. When every window is copied and verified it marks
the job done and pauses its own cadence. It never swaps, verifies the old table
or drops it: those stay explicit.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from ..timeseries import adopt_service as svc
    from .adopt_timeseries import _contract

    table = str(params.get("table") or "").strip()
    store = svc.JobStore(ctx.state_dir)
    job = store.get(table)
    if job is None:
        return SkillResult(ok=True, value={"status": "idle"},
                           actions_taken=[f"{table or '(no table)'}: no adoption job; nothing to do"])
    try:
        contract, _ = _contract({**job.params, "table": table})
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    out = svc.run_tick(job, contract, store)
    # A finished or halted job pauses its cadence now, not at the next restart.
    if out.status in ("done", "halted", "idle"):
        try:
            svc.sync_cadences(ctx.state_dir)
            from ..timeseries import adopt_watch

            adopt_watch.sync_watch_cadence(ctx.state_dir)
        except Exception as exc:  # noqa: BLE001 - the job file already says done/halted
            out.message += f" (cadence not paused: {exc})"
    value = {"status": out.status, "windows_run": out.windows_run, "reconnects": out.reconnects,
             "progress": out.progress}
    action = f"{table}: {out.status}" + (f", {out.message}" if out.message else "")
    if out.ok:
        return SkillResult(ok=True, value=value, actions_taken=[action])
    return SkillResult(ok=False, value=value, actions_taken=[action], errors=[action])


__all__ = ["run"]
