# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A declared fix must name a capability that actually exists.

The whole point of putting a Fix button on a case is that pressing it
does something. A button wired to a capability nobody registered is the
worst kind of dead affordance, because it does not look broken — it
looks like the product works, right up until a person is relying on it.

This is the "declared surface must exist" rule applied to remedies, and
it is a build-time check rather than a runtime one for the same reason:
the moment to find out is before shipping, not when someone is trying to
fix a node.
"""

from __future__ import annotations

from axiom.extensions.builtins.receipts.conditions import CONDITIONS
from axiom.extensions.builtins.receipts.skills import bind_default, bind_remedy_capabilities
from axiom.infra.skills import SkillRegistry


def test_every_declared_remedy_names_a_capability_that_exists():
    missing = bind_remedy_capabilities(SkillRegistry())
    assert missing == [], (
        f"declared remedies name capabilities that cannot be bound: {missing}. "
        "Either register the capability or remove the remedy — a Fix button "
        "wired to nothing looks like a working product."
    )


def test_the_receipts_registry_can_run_what_it_offers():
    registry = bind_default()
    assert registry.has("receipts.decide"), (
        "receipts.decide is declared in the manifest; a surface that calls it "
        "directly instead of through the registry skips the gateway entirely."
    )
    for condition in CONDITIONS.values():
        if condition.remedy is not None:
            assert registry.has(condition.remedy.capability)


def test_the_guard_can_fail():
    """The negative control: a remedy naming a capability nobody owns is
    reported, rather than passing quietly."""
    from axiom.extensions.builtins.receipts.conditions import Condition, Remedy

    real = dict(CONDITIONS)
    try:
        CONDITIONS[("invented", "stale")] = Condition(
            headline="{entity}: invented",
            detail="",
            fix="",
            remedy=Remedy(capability="nosuchextension.nosuchverb"),
        )
        assert bind_remedy_capabilities(SkillRegistry()) == ["nosuchextension.nosuchverb"]
    finally:
        CONDITIONS.clear()
        CONDITIONS.update(real)
