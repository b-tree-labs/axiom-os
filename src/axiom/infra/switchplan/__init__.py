# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The switch runner (ADR-197): build beside, prove, commit under a budget, soak, retire.

A domain implements ``SwitchHooks`` (prepare, probe, commit, rollback) and hands it to
:class:`SwitchPlan`; the lifecycle, the gate, the lock, the change record and the
refusals come with it. Domain-agnostic: nothing here knows what is being switched.
"""

from axiom.infra.switchplan.errors import (
    BudgetUnmeetable,
    GateNotPassed,
    ProbeFailed,
    RollbackFailed,
    SwitchBusy,
    SwitchRefused,
)
from axiom.infra.switchplan.ledger import FileLedger, PgLedger, SwitchRecord
from axiom.infra.switchplan.lock import SingleFlight
from axiom.infra.switchplan.plan import (
    CommitScope,
    FollowUps,
    LockBudget,
    PlanContext,
    SwitchHooks,
    SwitchPlan,
)
from axiom.infra.switchplan.probes import (
    REGISTRY,
    Probe,
    ProbeRegistry,
    ProbeResult,
    evaluate,
    wait_for,
)

__all__ = [
    "REGISTRY", "BudgetUnmeetable", "CommitScope", "FileLedger", "FollowUps", "GateNotPassed", "LockBudget",
    "PgLedger", "PlanContext", "Probe", "ProbeFailed", "ProbeRegistry", "ProbeResult", "RollbackFailed",
    "SingleFlight", "SwitchBusy", "SwitchHooks", "SwitchPlan", "SwitchRecord", "SwitchRefused", "evaluate", "wait_for",
]
