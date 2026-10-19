# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`process.sweep`: find rogue processes and runaway agents.

Report-only by default. `auto` acts on the policy class only
(`process.containment.auto`, default: orphans of tool/test subprocesses past
2x their max runtime). `kill` acts on every killable finding.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.process_containment import ContainmentPolicy
from axiom.infra.process_containment import sweep as run_sweep
from axiom.infra.skills import SkillContext, SkillResult

from ._common import next_steps, registry_for


def _agent_inputs(state_dir: Any) -> tuple[list[dict] | None, set[str] | None]:
    try:
        from axiom.extensions.builtins.agents.consent import load_consent
        from axiom.policy.action_ledger import ActionLedger

        enabled = set(load_consent().enabled)
        return ActionLedger(state_dir=state_dir).query(limit=1000), enabled
    except Exception:  # noqa: BLE001 - agent detection is best-effort; process checks still run
        return None, None


def sweep(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    reg = registry_for(ctx)
    mode = "kill" if params.get("kill") else "auto" if params.get("auto") else "report"
    rows, enabled = _agent_inputs(reg.state_dir)
    out = run_sweep(reg, mode=mode, policy=ContainmentPolicy.load(),
                    ledger_rows=rows, enabled_agents=enabled)
    killable = [f for f in out["findings"] if f["killable"]]
    steps: list[str] = []
    if mode == "report" and killable:
        steps.append("{cli} process sweep --auto      act on the policy class only")
        steps.append("{cli} process sweep --kill      stop every killable finding above")
        steps.append("{cli} process show <id>         inspect one before deciding")
    elif mode == "report":
        steps.append("{cli} process ls                nothing to contain; list what is registered")
    else:
        steps.append("{cli} process ls                confirm the result")
        steps.append("{cli} audit actions --agent process-containment   review what was done")
    return SkillResult(ok=True, value={**out, "next": next_steps(*steps)},
                       actions_taken=[a.get("action", "") for a in out["actions"]])
