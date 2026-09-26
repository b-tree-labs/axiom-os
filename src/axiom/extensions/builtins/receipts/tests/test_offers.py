# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An action a person cannot justify is an action they should not be
offered one click from.

Founder (2026-09-25): "the card doesn't give enough background to
responsibly resolve with the options presented. this encourages
recklessness."

So an offer carries what pressing it will do AND what it will not. The
second half is the one that matters: Acknowledge stops the asking
without changing the situation, and a person who reads it as "handled"
has been misled by a button.
"""

from __future__ import annotations

from axiom.extensions.builtins.receipts.brief import OversightItem
from axiom.extensions.builtins.receipts.cases import compose_cases
from axiom.extensions.builtins.receipts.offers import offers_for


def _case(**kw):
    case = compose_cases(
        [
            OversightItem(
                entity_kind="node",
                entity_id="node-a",
                claim_kind="heartbeat",
                status="stale",
                evidence="The last report arrived 2 hours ago.",
                site="s",
            )
        ]
    )[0]
    from dataclasses import replace

    return replace(case, **kw)


def test_a_fix_names_the_capability_it_will_run():
    """A person about to run something on their own infrastructure is
    owed the name of what runs, not just a friendly verb."""
    case = _case(
        handling={
            "can_run": True,
            "fix_summary": "Make this node report now.",
            "runs": "fleet.report",
            "needs_person": False,
            "because": "",
            "reason": "",
        }
    )
    fix = next(o for o in offers_for(case) if o.choice == "fix")
    assert fix.detail == "make this node report now"
    assert fix.runs == "fleet.report"


def test_acknowledge_says_what_it_does_not_do():
    """The recklessness guard. Acknowledge looks like resolution and is
    not: it stops the asking and changes nothing."""
    ack = next(o for o in offers_for(_case()) if o.choice == "acknowledge")
    assert ack.caveat == "This does not change the situation — only the asking."


def test_hold_says_nothing_acts_in_the_meantime():
    hold = next(o for o in offers_for(_case()) if o.choice == "hold")
    assert hold.detail == "stay quiet for a day, then ask again"
    assert hold.caveat == "Nothing acts on it in the meantime."


def test_discuss_carries_no_caveat_because_it_changes_nothing():
    discuss = next(o for o in offers_for(_case()) if o.choice == "discuss")
    assert discuss.records is False
    assert discuss.caveat == ""
    assert discuss.runs == ""
