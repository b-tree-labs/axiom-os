# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The PolicySourceRegistry combiner (ADR-083, build P2).

decide()'s policy layer (substrate, rules, graduation, default) is now a registry
of sources combined by one explicit algorithm: **deny-overrides**, then the
highest-priority positive opinion, then a propose fallback. These tests pin that
algorithm with synthetic sources, independent of the concrete backends.
"""

from __future__ import annotations

from axiom.extensions.builtins.authz.policy import (
    ABSTAIN,
    CombinedDecision,
    PolicyEffect,
    PolicyOpinion,
    PolicySourceRegistry,
)


class _FakeSource:
    def __init__(self, name, priority, opinion, *, may_deny=True):
        self.name = name
        self.priority = priority
        self.may_deny = may_deny
        self._opinion = opinion
        self.evaluated = False

    def evaluate(self, envelope, ctx, now):
        self.evaluated = True
        return self._opinion


def _permit(reason="ok"):
    return PolicyOpinion(PolicyEffect.PERMIT, reason)


def _deny(reason="no"):
    return PolicyOpinion(PolicyEffect.DENY, reason)


def _propose(reason="ask"):
    return PolicyOpinion(PolicyEffect.PROPOSE, reason)


def _combine(*sources):
    return PolicySourceRegistry(sources).combine(envelope=None, ctx=None, now=None)


def test_all_abstain_falls_back_to_propose():
    result = _combine(_FakeSource("a", 100, ABSTAIN), _FakeSource("b", 10, ABSTAIN))
    assert result.effect is PolicyEffect.PROPOSE
    assert result.source == "default"


def test_single_permit_wins():
    result = _combine(_FakeSource("sub", 50, _permit("granted")))
    assert result.effect is PolicyEffect.PERMIT
    assert result.reason == "granted"
    assert result.source == "sub"


def test_deny_overrides_a_higher_priority_permit():
    # The denier is LOWER priority, yet deny still wins — safety first.
    result = _combine(
        _FakeSource("rules", 100, _permit()),
        _FakeSource("sub", 50, _deny("substrate said no")),
    )
    assert result.effect is PolicyEffect.DENY
    assert result.reason == "substrate said no"


def test_highest_priority_positive_wins_when_no_deny():
    # rule PROPOSE (100) beats substrate PERMIT (50): a curated rule outranks a
    # relationship grant. Neither denies, so it is a priority contest.
    result = _combine(
        _FakeSource("rules", 100, _propose("needs review")),
        _FakeSource("sub", 50, _permit()),
    )
    assert result.effect is PolicyEffect.PROPOSE
    assert result.source == "rules"


def test_substrate_permit_beats_the_propose_default():
    # The authoritative-ALLOW property: a lone substrate PERMIT short-circuits the
    # novel-action propose fallback.
    result = _combine(
        _FakeSource("rules", 100, ABSTAIN),
        _FakeSource("sub", 50, _permit("relationship grant")),
        _FakeSource("grad", 10, ABSTAIN, may_deny=False),
    )
    assert result.effect is PolicyEffect.PERMIT
    assert result.source == "sub"


def test_non_may_deny_source_not_evaluated_when_higher_source_decides():
    # Efficiency: a lower-priority non-deny source (e.g. a graduation DB lookup)
    # is skipped once a higher source has produced a positive opinion.
    grad = _FakeSource("grad", 10, _permit(), may_deny=False)
    _combine(_FakeSource("sub", 50, _permit("first")), grad)
    assert grad.evaluated is False


def test_may_deny_sources_always_evaluated_even_after_a_permit():
    # Deny-overrides requires every may_deny source to be consulted before any
    # permit is honoured.
    denier = _FakeSource("sub", 50, _deny(), may_deny=True)
    _combine(_FakeSource("rules", 100, _permit()), denier)
    assert denier.evaluated is True


def test_highest_priority_denier_reason_is_reported():
    result = _combine(
        _FakeSource("rules", 100, _deny("rule deny")),
        _FakeSource("sub", 50, _deny("sub deny")),
    )
    assert result.effect is PolicyEffect.DENY
    assert result.reason == "rule deny"  # higher priority denier


def test_register_orders_by_priority():
    reg = PolicySourceRegistry([_FakeSource("low", 10, _permit("low"))])
    reg.register(_FakeSource("high", 100, _permit("high")))
    result = reg.combine(envelope=None, ctx=None, now=None)
    assert result.reason == "high"


def test_combined_decision_shape():
    result = _combine(_FakeSource("sub", 50, PolicyOpinion(
        PolicyEffect.PERMIT, "ok", ("r1", "r2"))))
    assert isinstance(result, CombinedDecision)
    assert result.matched_rules == ("r1", "r2")
