# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What the switch runner refuses, and why (ADR-197 D3).

Every refusal changes nothing. The message names the missing property and the fix.
"""

from __future__ import annotations


class SwitchRefused(Exception):
    """The step cannot run safely as things stand."""


class SwitchBusy(SwitchRefused):
    """Another run holds this switch's single-flight lock."""


class GateNotPassed(SwitchRefused):
    """A commit was asked for without a passing prove, or a probe failed."""


class BudgetUnmeetable(SwitchRefused):
    """The commit is expected to hold its lock longer than the budget allows."""


class ProbeFailed(GateNotPassed):
    """A declared probe failed or could not be evaluated. Raised inside a
    commit's transaction it rolls the commit back."""


class RollbackFailed(Exception):
    """The rollback hook raised. The original failure is the cause."""
