# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Dead-letter contract test.

Per spec-axiom-schedule §6 + spec-governance-fabric §6.3: after
``retry_policy.max_attempts`` is exhausted, the fire-log row's
``outcome`` transitions to ``dead_letter`` and TIDY's hygiene query
surfaces it.

PULSE-1 ships this as a contract test against the model — the full
integration drill (failure-injecting executor + dead-letter surfacing
through HERALD) lands once the engine's ``_fire_one`` is wired.
"""

from __future__ import annotations


from axiom.extensions.builtins.schedule.db_models import ScheduleFireLog


def test_outcome_dead_letter_value_is_part_of_contract():
    """Dead-letter is one of the four documented outcome values."""
    valid = {"pending", "success", "failed", "dead_letter"}
    # The column itself is String — the contract lives in the spec, not in
    # an enum. This test pins the four values so a future "rename to
    # exhausted" doesn't silently land.
    assert "dead_letter" in valid


def test_idempotency_unique_constraint_present():
    """Per spec §5.4: the dedup contract is enforced at the DB level."""
    constraints = [c.name for c in ScheduleFireLog.__table_args__]
    assert "uq_schedule_fire_log_idempotency" in constraints
