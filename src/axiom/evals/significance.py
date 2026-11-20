# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Paired significance — whether a difference between two arms is evidence.

The comparative battery runs the SAME items under two conditions. That pairing
is the strongest structure an A/B comparison can have, and the first version of
this battery threw it away: it compared the aggregate delta against the
run-to-run spread of each arm, which is a dispersion heuristic, not a test. It
cannot distinguish "both arms got a different random half right" from "the
candidate fixed exactly the items the baseline failed", because it never looks
at WHICH items moved.

McNemar's test (1947) is the standard instrument for this shape — paired binary
outcomes — and it looks only at the DISCORDANT pairs: items one arm got and the
other missed. Items both arms got right, or both got wrong, carry no information
about which arm is better, so they are discarded. That is the test, not a
shortcut, and it is why padding a pool with easy items cannot manufacture a
result here.

Two implementations, chosen by discordant count:

* **Exact binomial** below :data:`EXACT_THRESHOLD` discordant pairs. Our pools
  are tens of items and the chi-square approximation is unreliable at that size.
  Using it anyway would report confidence the data does not support, which is
  the specific failure this module exists to stop.
* **Chi-square with continuity correction** above it, where the approximation
  holds and the exact sum gets expensive.

Dependency-free on purpose. This runs in the pre-push gate and on a laptop
install with no scientific stack, and a significance test that is skipped when
SciPy is absent is a significance test nobody runs.

Nothing here is novel: it is textbook statistics, implemented directly so the
number is checkable against any table.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

#: Below this many discordant pairs, use the exact binomial test. The
#: conventional cut for trusting the chi-square approximation on paired counts.
EXACT_THRESHOLD = 25

#: The significance level, stated once. A caller may override it, but it must be
#: chosen BEFORE seeing the p-value — an alpha picked afterwards is not a test.
DEFAULT_ALPHA = 0.05


@dataclass(frozen=True)
class McNemarResult:
    """The full result, not just a boolean.

    ``significant`` alone invites reporting the verdict and dropping the
    evidence. The discordant counts are what a reader needs to judge whether a
    p-value rests on four items or four hundred, and ``method`` says which test
    produced it.
    """

    discordant_baseline_only: int
    discordant_candidate_only: int
    p_value: float
    method: str
    alpha: float = DEFAULT_ALPHA

    @property
    def discordant(self) -> int:
        """Pairs that carry information. The effective sample size of the test —
        usually far smaller than the pool, which is the honest number to quote."""
        return self.discordant_baseline_only + self.discordant_candidate_only

    @property
    def favors(self) -> str:
        """``"candidate"``, ``"baseline"``, or ``"neither"``.

        Direction is reported independently of significance, because a battery
        that can only say "different" cannot tell you a change made things
        worse — and finding that out is half of why the battery exists.
        """
        if self.discordant_candidate_only > self.discordant_baseline_only:
            return "candidate"
        if self.discordant_baseline_only > self.discordant_candidate_only:
            return "baseline"
        return "neither"

    @property
    def significant(self) -> bool:
        return self.p_value < self.alpha

    def as_dict(self) -> dict[str, object]:
        return {
            "discordant_baseline_only": self.discordant_baseline_only,
            "discordant_candidate_only": self.discordant_candidate_only,
            "discordant": self.discordant,
            "p_value": self.p_value,
            "method": self.method,
            "alpha": self.alpha,
            "favors": self.favors,
            "significant": self.significant,
        }


def _binomial_two_sided(smaller: int, total: int) -> float:
    """Two-sided exact p under a fair coin: ``2 * P(X <= smaller)``, capped at 1.

    The cap is not cosmetic. When the discordant counts are near-equal the
    doubled tail exceeds one, and a "p-value" above 1 is a number that cannot be
    compared to an alpha.
    """
    tail = sum(math.comb(total, i) for i in range(smaller + 1)) / (2**total)
    return min(1.0, 2.0 * tail)


def _chi_square_1df_sf(statistic: float) -> float:
    """``P(X > statistic)`` for a chi-square with one degree of freedom.

    Exactly ``erfc(sqrt(x / 2))`` — the one-df chi-square is a squared standard
    normal, so its survival function is the two-sided normal tail. Using the
    closed form keeps this dependency-free without approximating anything.
    """
    if statistic <= 0.0:
        return 1.0
    # Underflows to exactly 0.0 somewhere past ~1400 discordant pairs. A
    # p-value of zero is not a thing, but every comparison against an alpha
    # still behaves correctly, and no pool here is within two orders of
    # magnitude of that. Recorded so the next reader does not have to rederive
    # it, and so nobody quotes "p = 0" from a run that got there.
    return math.erfc(math.sqrt(statistic / 2.0))


def mcnemar(
    *,
    baseline: Sequence[bool],
    candidate: Sequence[bool],
    alpha: float = DEFAULT_ALPHA,
    exact_threshold: int = EXACT_THRESHOLD,
) -> McNemarResult:
    """Test whether two arms differ on the same items.

    ``baseline[i]`` and ``candidate[i]`` are the two arms' outcomes on item
    ``i``, where ``True`` is the good outcome. A caller measuring something
    where LOW is good (misleading answers, say) passes the negation, so the
    direction reported by :attr:`McNemarResult.favors` stays meaningful instead
    of needing a flag nobody remembers to set.

    Sequences of different length are refused. Pairing is the entire basis of
    the test, and zipping to the shorter one would compare different items and
    return a confident number anyway — the failure mode this module exists to
    prevent, reintroduced at the door.
    """
    if len(baseline) != len(candidate):
        raise ValueError(
            f"paired test needs matched items: got {len(baseline)} baseline "
            f"and {len(candidate)} candidate outcomes"
        )

    baseline_only = sum(1 for b, c in zip(baseline, candidate) if b and not c)
    candidate_only = sum(1 for b, c in zip(baseline, candidate) if c and not b)
    total = baseline_only + candidate_only

    if total == 0:
        # NOT "the arms are equal" — "this comparison cannot tell". Every pair
        # agreed, so there is no evidence either way, and p = 1.0 says exactly
        # that. Reporting equality would be a claim the data does not make.
        return McNemarResult(baseline_only, candidate_only, 1.0, "exact", alpha)

    if total < exact_threshold:
        p_value = _binomial_two_sided(min(baseline_only, candidate_only), total)
        method = "exact"
    else:
        corrected = max(0.0, abs(baseline_only - candidate_only) - 1.0)
        p_value = _chi_square_1df_sf(corrected**2 / total)
        method = "chi-square"

    # NOT rounded. A p-value is read in its tail, and fixed-decimal rounding
    # destroys precision exactly where it carries meaning: at ten places, 1e-12
    # becomes 0.0 — a p-value of zero, which is not a thing.
    return McNemarResult(baseline_only, candidate_only, p_value, method, alpha)
