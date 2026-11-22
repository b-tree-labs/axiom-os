# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Budget envelope: how much a capability may consume, not just what it may do.

Prerequisite A0 of ADR-232 part 5 and prd-capability-budgets (R1 to R4).
A ``CapabilityToken`` already scopes WHAT an actor may do and UNTIL WHEN. The
envelope scopes HOW MUCH, in four metered limits: model turns, tokens, an
estimated dollar figure, and wall-clock seconds.

Two types, because a token is frozen and a meter is not:

* :class:`BudgetEnvelope` is an immutable grant. It rides on the token
  (``CapabilityToken.budget``) and never changes once minted.
* :class:`BudgetLedger` is the running account for one grant. It records what
  has been drawn, hands out slices to children, takes unused amounts back, and
  can be read at any time without touching the token.

Rules the ledger enforces (ADR-232 part 5):

* Draws happen at step boundaries. A draw is refused once ANY limit has no
  remaining amount. A single step that begins with something left may
  overshoot; it is never cut off mid-action (prd R4). The next draw is the one
  that is denied.
* Exhaustion is returned as data, ``unmet: budget_exhausted`` naming the limit,
  in the ``{id, why, fix}`` shape of ADR-185. Nothing raises across a surface.
* A child's grant is the INTERSECTION of the parent's remaining amounts and
  what the child asked for: a child can only lower. The grant is reserved out
  of the parent at spawn and the unused part is refunded when the child
  finishes, so a tree can never spend more than its root grant.
* Dollars are an estimate (prd R3), carried in the same arithmetic as the
  native units but never presented as billing-exact.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

#: The four metered limits, in the order they are reported.
LIMITS: tuple[str, ...] = ("turns", "tokens", "dollars", "wall_clock_seconds")

#: Unmet ids (ADR-185 ``{id, why, fix}``).
BUDGET_EXHAUSTED = "budget_exhausted"
BUDGET_REQUIRED = "budget_required"


@dataclass(frozen=True)
class BudgetEnvelope:
    """An immutable grant across the four limits. Every limit is a cap, >= 0."""

    turns: float
    tokens: float
    dollars: float
    wall_clock_seconds: float

    def __post_init__(self) -> None:
        for name in LIMITS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(
                    f"budget limit {name!r} must be a number, got {type(value).__name__}"
                )
            if value != value or value < 0:  # NaN or negative
                raise ValueError(f"budget limit {name!r} must be >= 0, got {value!r}")

    def as_dict(self) -> dict[str, float]:
        return {name: getattr(self, name) for name in LIMITS}

    def intersect(self, other: BudgetEnvelope) -> BudgetEnvelope:
        """The smaller of the two on every limit. Never larger than either."""
        return BudgetEnvelope(**{n: min(getattr(self, n), getattr(other, n)) for n in LIMITS})

    def within(self, other: BudgetEnvelope) -> bool:
        """True when this envelope is at most ``other`` on every limit."""
        return all(getattr(self, n) <= getattr(other, n) for n in LIMITS)

    def minus(self, other: BudgetEnvelope) -> BudgetEnvelope:
        """Per-limit subtraction, floored at zero."""
        return BudgetEnvelope(**{n: max(0, getattr(self, n) - getattr(other, n)) for n in LIMITS})

    def plus(self, other: BudgetEnvelope) -> BudgetEnvelope:
        return BudgetEnvelope(**{n: getattr(self, n) + getattr(other, n) for n in LIMITS})

    @classmethod
    def zero(cls) -> BudgetEnvelope:
        return cls(0, 0, 0, 0)


def intersect_request(
    remaining: BudgetEnvelope, requested: BudgetEnvelope | None
) -> BudgetEnvelope:
    """A child's grant: ``requested`` clamped to the parent's ``remaining``.

    ``None`` means "no preference" and yields everything that remains, which a
    caller may then lower but never raise.
    """
    return remaining if requested is None else remaining.intersect(requested)


def exhausted_unmet(limit: str, granted: float, drawn: float) -> dict[str, str]:
    """The ``unmet`` entry for an exhausted limit (ADR-185 shape)."""
    return {
        "id": BUDGET_EXHAUSTED,
        "why": f"budget limit {limit!r} is exhausted (granted {granted:g}, drawn {drawn:g})",
        "fix": "ask the owner to grant a larger budget, or start a new run with a smaller task",
    }


def required_unmet() -> dict[str, str]:
    """The ``unmet`` entry for an agent-backed run asked to start with no envelope."""
    return {
        "id": BUDGET_REQUIRED,
        "why": "an agent-backed run needs a budget envelope (turns, tokens, dollars, wall_clock_seconds)",
        "fix": "mint or delegate a capability token that carries a budget envelope",
    }


@dataclass(frozen=True)
class DrawOutcome:
    """The result of one draw. ``ok`` False carries the ``unmet`` entry; no raise."""

    ok: bool
    unmet: tuple[dict[str, str], ...] = ()
    limit: str | None = None


class BudgetLedger:
    """The running account for one :class:`BudgetEnvelope` grant.

    Thread-safe. ``granted`` never changes; ``drawn`` and ``reserved`` (the
    portion handed to live children) move. ``remaining`` is always
    ``granted - drawn - reserved``, floored at zero.
    """

    def __init__(
        self,
        granted: BudgetEnvelope,
        *,
        parent: BudgetLedger | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._granted = granted
        self._parent = parent
        self._clock = clock
        self._lock = threading.RLock()
        self._drawn = dict.fromkeys(LIMITS, 0.0)
        self._reserved = dict.fromkeys(LIMITS, 0.0)
        self._last_tick = clock()
        self._exhausted: str | None = None
        self._closed = False

    # ---- reads (never mutate the token or the grant) ---------------------

    @property
    def granted(self) -> BudgetEnvelope:
        return self._granted

    def drawn(self) -> BudgetEnvelope:
        with self._lock:
            return BudgetEnvelope(**self._drawn)

    def remaining(self) -> BudgetEnvelope:
        with self._lock:
            return BudgetEnvelope(
                **{
                    n: max(0.0, getattr(self._granted, n) - self._drawn[n] - self._reserved[n])
                    for n in LIMITS
                }
            )

    @property
    def exhausted_limit(self) -> str | None:
        """The first limit found exhausted, remembered for the record."""
        with self._lock:
            return self._exhausted

    def snapshot(self) -> dict[str, Any]:
        """Granted, drawn, remaining and outcome, for the run record and provenance."""
        with self._lock:
            return {
                "granted": self._granted.as_dict(),
                "drawn": self.drawn().as_dict(),
                "remaining": self.remaining().as_dict(),
                "outcome": BUDGET_EXHAUSTED if self._exhausted else "within_budget",
                "exhausted_limit": self._exhausted,
            }

    # ---- draws -----------------------------------------------------------

    def check(self) -> DrawOutcome:
        """Is there anything left on every limit? Does not draw."""
        with self._lock:
            for n in LIMITS:
                if getattr(self.remaining(), n) <= 0:
                    return self._refuse(n)
            return DrawOutcome(ok=True)

    def draw(
        self,
        *,
        turns: float = 0,
        tokens: float = 0,
        dollars: float = 0,
        wall_clock_seconds: float = 0,
    ) -> DrawOutcome:
        """Draw at a step boundary.

        Refused (as data) when any limit is already exhausted. Otherwise the
        amounts are recorded in full, even if they overshoot a limit: the step
        that was allowed to begin is allowed to finish, and the NEXT draw is the
        one that is refused.
        """
        amounts = {
            "turns": turns,
            "tokens": tokens,
            "dollars": dollars,
            "wall_clock_seconds": wall_clock_seconds,
        }
        for n, v in amounts.items():
            if v < 0 or v != v:
                raise ValueError(f"draw amount {n!r} must be >= 0, got {v!r}")
        with self._lock:
            gate = self.check()
            if not gate.ok:
                return gate
            for n, v in amounts.items():
                self._drawn[n] += v
            return DrawOutcome(ok=True)

    def tick(self) -> DrawOutcome:
        """Draw the wall-clock time elapsed since the last tick (or construction)."""
        with self._lock:
            now = self._clock()
            elapsed = max(0.0, now - self._last_tick)
            self._last_tick = now
            # Elapsed time is a fact, not a request: record it even when it
            # overshoots, then report exhaustion so the NEXT step is denied.
            self._drawn["wall_clock_seconds"] += elapsed
            return self.check()

    def _refuse(self, limit: str) -> DrawOutcome:
        self._exhausted = self._exhausted or limit
        entry = exhausted_unmet(
            limit, getattr(self._granted, limit), self._drawn[limit] + self._reserved[limit]
        )
        return DrawOutcome(ok=False, unmet=(entry,), limit=limit)

    # ---- slices ----------------------------------------------------------

    def slice(self, requested: BudgetEnvelope | None = None) -> BudgetLedger:
        """Reserve a child grant out of this ledger and return the child's ledger.

        The grant is ``requested`` intersected with what remains here, so a child
        can only lower. The reserved amount leaves this ledger's remaining
        immediately and returns, less what the child used, at :meth:`close`.
        """
        with self._lock:
            grant = intersect_request(self.remaining(), requested)
            for n in LIMITS:
                self._reserved[n] += getattr(grant, n)
            return BudgetLedger(grant, parent=self, clock=self._clock)

    def close(self) -> BudgetEnvelope:
        """Finish this ledger: refund the unused part to the parent. Idempotent.

        Returns the amount refunded. The child's drawn amounts stay drawn on the
        parent, because they were spent.
        """
        with self._lock:
            if self._closed:
                return BudgetEnvelope.zero()
            self._closed = True
            unused = self.remaining()
            parent = self._parent
        if parent is None:
            return unused
        with parent._lock:
            for n in LIMITS:
                spent = self._drawn[n]
                parent._reserved[n] = max(0.0, parent._reserved[n] - getattr(self._granted, n))
                parent._drawn[n] += spent
        return unused


def ledger_for(
    budget: BudgetEnvelope | None,
) -> tuple[BudgetLedger | None, tuple[dict[str, str], ...]]:
    """A ledger for an envelope, or ``(None, (budget_required,))`` when absent."""
    if budget is None:
        return None, (required_unmet(),)
    return BudgetLedger(budget), ()


__all__ = [
    "BUDGET_EXHAUSTED",
    "BUDGET_REQUIRED",
    "LIMITS",
    "BudgetEnvelope",
    "BudgetLedger",
    "DrawOutcome",
    "exhausted_unmet",
    "intersect_request",
    "ledger_for",
    "required_unmet",
]
