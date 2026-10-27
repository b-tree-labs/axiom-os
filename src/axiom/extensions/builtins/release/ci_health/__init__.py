# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET CI health: detect Actions waste, fix what is safe, prevent the rest.

* ``gather`` reads spend (billing usage), runs, and selection markers.
* ``sweep`` adds the automatic fixes: cancel superseded runs, record the
  budget state landing obeys, alert through HERALD.
* ``lint`` flags workflow shapes that waste minutes.

Every forge call goes through ``CIClient``; tests pass fixtures.
"""

from axiom.extensions.builtins.release.ci_health.budget import (
    LandingPolicy,
    batch_gate,
    budget_state,
    current_state,
    landing_policy,
)
from axiom.extensions.builtins.release.ci_health.cancel import CancelDecision, plan_cancels
from axiom.extensions.builtins.release.ci_health.client import CIClient, CIClientError, GhClient
from axiom.extensions.builtins.release.ci_health.config import CIHealthConfig, load_ci_health, parse_ci_health
from axiom.extensions.builtins.release.ci_health.lint import LintFinding, lint_dir, lint_workflow
from axiom.extensions.builtins.release.ci_health.report import CIHealthReport, gather, render_digest, render_text, sweep
from axiom.extensions.builtins.release.ci_health.update_branch import UpdatePlan, plan_update_branch

__all__ = [
    "CIClient",
    "CIClientError",
    "CIHealthConfig",
    "CIHealthReport",
    "CancelDecision",
    "GhClient",
    "LandingPolicy",
    "LintFinding",
    "UpdatePlan",
    "batch_gate",
    "budget_state",
    "current_state",
    "gather",
    "landing_policy",
    "lint_dir",
    "lint_workflow",
    "load_ci_health",
    "parse_ci_health",
    "plan_cancels",
    "plan_update_branch",
    "render_digest",
    "render_text",
    "sweep",
]
