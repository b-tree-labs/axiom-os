# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Uncertainty that composes — a platform primitive, not an extension.

Founder direction (2026-09-28): *"uncertainty should be infinitely
composable. We don't know how various extensions will contribute to it and
they need documented Axiom guidance for how to provide it. Uncertainty also
needs to establish its context and relativity (to some assertion)."*

Why a scalar cannot be the carried form
---------------------------------------
``silver.signals.uncertainty`` is a ``double precision`` in the value's own
unit, and that column is right for reading — an error bar, a human, a chart.
It is a dead end for COMPOSING, and not through neglect. GUM's law of
propagation carries a covariance term, and two standard uncertainties cannot
be combined without knowing their correlation. Forty independent readings
combine as root-sum-square; forty sharing one calibration offset combine
linearly; a log-mean temperature difference aggregated beside the
sensors it was computed from is double-counted outright. Same numbers,
three answers, and nothing in a scalar says which.

So the third member of a family this codebase already keeps:

    A value without its unit is not a fact.
    A ratio without its reference is not a fact.
    **An uncertainty without its correlation structure is not composable.**

The closed algebra
------------------
A quantity is carried as an AFFINE FORM over named independent sources:

    x = x₀ + a₁ε₁ + a₂ε₂ + … + aₙεₙ

Each ``εᵢ`` is a globally named, independent unit-variance source; ``aᵢ`` is
this quantity's sensitivity to it. Then ``u(x) = √(Σaᵢ²)`` and the
correlation between any two quantities is ``Σaᵢbᵢ / (u(x)u(y))`` — COMPUTED
from shared symbols, never declared and never stored.

This is affine arithmetic (Comba & Stolfi 1993; de Figueiredo & Stolfi
2004), and closure is the point: combining two quantities yields another
quantity of the same type, at any depth, in any order, with no special case
and no correlation matrix to maintain. That is what "infinitely composable"
has to mean operationally. It was invented to fix the dependency problem
that makes plain interval arithmetic explode, which is the failure we would
otherwise walk into.

Nonlinear operations mint a NEW symbol for the linearisation residual, so
the approximation error is tracked as its own uncertainty rather than
quietly dropped.

Why extensions need no coordination
-----------------------------------
An extension declares symbols in a namespace it owns and nothing else.
Two channels that share ``signals:tc-14:calibration`` correlate correctly
without either contributor knowing the other exists. Composition needs only
the symbol NAMES, so it keeps working when the declaring extension is not
installed — the map is self-contained and the registry below carries
metadata, never arithmetic. That is the resilience requirement: a missing
extension degrades what you can EXPLAIN, never what you can COMPUTE.

Context and relativity
----------------------
A bare ``u`` is uninterpretable. GUM is precise about what must travel with
it, and :class:`Budget` carries exactly that: the MEASURAND the uncertainty
is about (without which it is not defined at all), the coverage factor,
whether it was evaluated statistically or by other means (Type A / Type B),
what it is traceable to, and the domain over which it holds.

That is GUM's *uncertainty budget*, and it is the shape this codebase
already has. A ``Derivation`` carries inputs, a rule, a check and a limit.
An uncertainty budget is inputs (symbols and magnitudes), a rule (the
propagation method), a check (coverage validation) and a limit (the validity
domain). An uncertainty IS a claim, and claims here already state what they
cannot establish.

Absence has kinds
-----------------
Three inputs are distinguishable and must stay so, because collapsing them
is how a measurement becomes a decoration:

- :class:`Quantity` — magnitude and structure known. Composes exactly.
- :class:`MagnitudeOnly` — ``u`` known, correlation unknown. Bounds.
- :class:`Unquantified` — nothing was reported. Cannot bound, and is
  COUNTED rather than assumed to be zero. Zero is a claim of perfect
  precision; silence is not.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from axiom.uncertainty.coverage import DEFAULT_CONFIDENCE, INFINITE_DOF

#: ``<extension>:<scope>:<source>`` — the namespace is the extension, per
#: founder direction. Two sites' identically-named instruments therefore do
#: NOT correlate unless a contributor deliberately scopes them together,
#: which is the safe default: inventing correlation understates uncertainty,
#: while missing it overstates, and only one of those misleads toward
#: confidence.
#: A source name, in one of exactly two forms.
#:
#: **Bare** — ``<extension>:<scope>:<source>``. A source belonging to THIS
#: node. Every symbol minted by an extension, and everything stored in the
#: companion table, is of this form.
#:
#: **Qualified** — ``@origin/<extension>:<scope>:<source>``. A source
#: belonging to another principal, as it arrives over the wire. The origin
#: is a principal in the platform's own ``@name:context`` form.
#:
#: Both are admitted here because both are legitimate and a revived foreign
#: quantity has to be constructible. What is NOT admitted is the two forms
#: being interchangeable: `axiom.uncertainty.wire` refuses to qualify a
#: symbol that already carries a different origin, and refuses an
#: unqualified symbol arriving from outside — because a bare foreign symbol
#: would match a local source of the same name and make two different
#: instruments look like one.
_BARE_SYMBOL = r"[a-z0-9_]+:[A-Za-z0-9_.\-]+:[A-Za-z0-9_.\-]+"
_ORIGIN_PREFIX = r"@[A-Za-z0-9_\-.]+(?::[A-Za-z0-9_\-.]+)?/"
_SYMBOL = re.compile(rf"^(?:{_ORIGIN_PREFIX})?{_BARE_SYMBOL}$")

#: Evaluated statistically from repeated observation (GUM Type A), or by
#: other means — a certificate, a specification, judgement (Type B). Both
#: are legitimate; conflating them hides which part more measurement could
#: reduce.
TYPE_A = "A"
TYPE_B = "B"


class SymbolError(ValueError):
    """A symbol that does not name its owner cannot be governed."""


def check_symbol(symbol: str) -> str:
    """Validate a bare or origin-qualified symbol; return it unchanged."""
    if not _SYMBOL.match(symbol or ""):
        raise SymbolError(
            f"{symbol!r} is not <extension>:<scope>:<source>, optionally prefixed "
            "'@origin/' — a symbol must name the extension that owns it, or nobody "
            "can say where an uncertainty came from or who may change it"
        )
    return symbol


@dataclass(frozen=True)
class Budget:
    """What an uncertainty is ABOUT, and relative to what.

    GUM's uncertainty budget. Held per symbol rather than per value: the
    sensor's calibration uncertainty is a property of the instrument,
    and every reading it produces refers to the same budget.
    """

    symbol: str
    #: The quantity intended to be measured. Without it an uncertainty is
    #: not merely unexplained, it is undefined — GUM's central point.
    measurand: str
    #: Standard uncertainty (k=1) in the measurand's own unit.
    standard: float
    kind: str = TYPE_B
    #: What the chain of comparisons ends at. "unstated" is honest and
    #: common; a fabricated reference is not.
    traceable_to: str = "unstated"
    #: Where this holds. A surrogate's error bound says nothing outside its
    #: training domain — the number there is not wrong, it is inapplicable,
    #: and that distinction is the one that matters most for models.
    valid_over: str = "unstated"
    #: Free-form, for what the fields above cannot carry.
    note: str = ""
    #: Degrees of freedom. A Type A component evaluated from ``n``
    #: observations has ``n − 1``; a Type B bound from a certificate or a
    #: tolerance is conventionally unlimited, which is the default.
    #:
    #: Not decoration — it sets the coverage factor. At six observations the
    #: honest 95% factor is 2.57, so quoting the habitual ``k = 2``
    #: understates the interval by 29%, which is invisible on a chart and
    #: decisive in a limit check.
    dof: float = INFINITE_DOF

    def __post_init__(self) -> None:
        check_symbol(self.symbol)
        if self.standard < 0:
            raise ValueError("a standard uncertainty is a magnitude; it cannot be negative")
        if self.kind not in (TYPE_A, TYPE_B):
            raise ValueError(f"kind must be {TYPE_A!r} (statistical) or {TYPE_B!r} (other)")
        if not self.measurand:
            raise ValueError(
                "a budget needs its measurand — an uncertainty about nothing in "
                "particular cannot be interpreted, combined or checked"
            )


@dataclass(frozen=True)
class Quantity:
    """A value whose uncertainty structure is known. Composes exactly."""

    value: float
    unit: str
    #: symbol → sensitivity. Empty means an exactly known constant, which is
    #: a positive claim and is different from :class:`Unquantified`.
    terms: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for s in self.terms:
            check_symbol(s)

    @property
    def u(self) -> float:
        """Combined standard uncertainty, k=1."""
        # math.hypot, not sqrt(sum of squares). It is the same quantity and
        # the stdlib one is overflow- and underflow-safe: sqrt(sum(a*a))
        # returns inf for coefficients near 1e200 and 0.0 for ones near
        # 1e-200, and so does np.linalg.norm. Measured, not assumed.
        return math.hypot(*self.terms.values())

    def coverage(
        self, *, confidence: float = DEFAULT_CONFIDENCE, dof: float = INFINITE_DOF
    ) -> tuple[float, float, float]:
        """``(low, high, k)`` — the interval and the factor that produced it.

        Prefer this to :meth:`expanded`. It takes the confidence level
        explicitly and derives ``k`` from the t-distribution at ``dof``,
        which is what GUM asks for. :meth:`expanded` takes ``k`` on faith,
        and the ``k = 2`` everybody writes is ~95% only as the degrees of
        freedom go to infinity.
        """
        from axiom.uncertainty.coverage import interval

        return interval(self.value, self.u, confidence=confidence, dof=dof)

    def expanded(self, k: float = 2.0) -> float:
        """U = k·u. Reporting an interval without stating k is the most
        common way an uncertainty becomes uninterpretable.

        Kept for the case where a caller genuinely has a ``k`` to apply.
        When the caller wants a confidence LEVEL, :meth:`coverage` is the
        right call: it derives ``k`` rather than assuming it.
        """
        return k * self.u

    def scaled(self, factor: float, *, unit: str | None = None) -> Quantity:
        """Unit conversion and any other exact linear rescale.

        Sensitivities scale with the value, which is what keeps ``value``
        and ``uncertainty`` in one unit through a W→MW conversion instead of
        letting them drift apart.
        """
        return Quantity(
            value=self.value * factor,
            unit=unit if unit is not None else self.unit,
            terms={s: a * factor for s, a in self.terms.items()},
        )


@dataclass(frozen=True)
class MagnitudeOnly:
    """``u`` is known; how it correlates with anything else is not.

    The common real case: a source reported an uncertainty without saying
    what it came from. Enough to BOUND a combination, never enough to
    compute one.
    """

    value: float
    unit: str
    u: float


@dataclass(frozen=True)
class Unquantified:
    """Nothing was reported. Counted, never assumed to be zero."""

    value: float
    unit: str


Input = Quantity | MagnitudeOnly | Unquantified


@dataclass(frozen=True)
class Combination:
    """The result of combining inputs, with what could not be established.

    ``low`` and ``high`` bracket the combined standard uncertainty, and the
    bracket is only as honest as its premise, so the premise is a field.

    ``high`` is unconditional. For any correlation whatsoever, the standard
    deviation of a sum cannot exceed the sum of the standard deviations —
    the triangle inequality in L². Nothing is assumed.

    ``low`` is NOT unconditional, and it took a proof to notice. It is the
    minimum under **non-negatively correlated** unstructured sources, where
    ``u² = Σuᵢ² + 2ΣΣuᵢuⱼrᵢⱼ`` is minimised at ``rᵢⱼ = 0`` and gives
    root-sum-square. Allow negative correlation and the true minimum is
    lower: two equal magnitudes at ``r = −1`` cancel to zero.

    ``low_unconstrained`` is that mathematically complete floor,
    ``max(0, 2·max(uᵢ) − Σuᵢ)``. It is reported and it is usually useless —
    frequently zero — which is why it is not the headline. A bound that is
    correct and carries no information is not more honest than a bound
    whose premise is stated; it is just unusable. So both travel, and
    :attr:`premise` names the one the headline rests on.

    Shared systematics — a calibration offset, a reference junction, a
    common power supply — are non-negatively correlated in practice, which
    is why the stated premise is the useful default rather than a
    convenience. Where a contributor knows of an anti-correlated pair, the
    answer is not a wider bound: it is to declare the structure and get an
    exact number.
    """

    value: float
    unit: str
    terms: Mapping[str, float]
    low: float
    high: float
    #: The mathematically complete floor, assuming nothing about sign.
    low_unconstrained: float = 0.0
    #: What ``low`` rests on. "exact" when nothing was assumed.
    premise: str = "exact"
    #: Inputs that reported no uncertainty at all. These are OUTSIDE the
    #: bound: nothing bounds an unreported quantity, and pretending
    #: otherwise would make the interval a decoration.
    unquantified: int = 0
    counted: int = 0

    @property
    def exact(self) -> bool:
        return self.low == self.high and not self.unquantified

    def reads(self) -> str:
        """The sentence a person gets. States what it could not establish."""
        head = f"{self.value:g} {self.unit}".strip()
        if self.unquantified:
            known = self.counted - self.unquantified
            if known <= 0:
                return f"{head} — no input reported an uncertainty, so none is claimed"
            body = f"± {self.low:g}" if self.exact else f"± between {self.low:g} and {self.high:g}"
            return (
                f"{head} {body} over {known} of {self.counted} inputs; "
                f"{self.unquantified} reported no uncertainty and are not in that range"
            )
        if self.exact:
            return f"{head} ± {self.low:g}"
        return (
            f"{head} ± between {self.low:g} and {self.high:g} — magnitudes are known, "
            f"how they correlate is not; the lower end assumes {self.premise}"
        )


def correlation(a: Quantity, b: Quantity) -> float:
    """Computed from shared symbols. Never stored, never declared.

    Each coefficient is normalised by its own quantity's magnitude BEFORE the
    products are formed, rather than dividing the finished dot product by
    ``ua * ub``. Both are algebraically the same ratio; only this one is
    computable across the full range.

    Dividing at the end fails at both extremes and fails SILENTLY, giving a
    plausible 0.0 for quantities that are in fact perfectly correlated:

    - coefficients near 1e-200 make every product underflow to 0.0, so the
      numerator vanishes;
    - coefficients near 1e200 make them overflow to inf, so the numerator is
      unrepresentable.

    Normalising first puts every term in [-1, 1], where products are exact
    and the sum is what it should be. Found by checking the extremes rather
    than by a test failing, since the property tests all use ordinary
    magnitudes and passed throughout.
    """
    ua, ub = a.u, b.u
    if ua == 0 or ub == 0:
        return 0.0
    # fsum over pre-scaled terms: correctly rounded, so a long shared-symbol
    # list cannot drift the coefficient out of [-1, 1] by accumulated
    # rounding either.
    shared = math.fsum(
        (a.terms.get(s, 0.0) / ua) * (b.terms.get(s, 0.0) / ub) for s in set(a.terms) | set(b.terms)
    )
    return max(-1.0, min(1.0, shared))


def _unit_of(inputs: Sequence[Input]) -> str:
    units = {i.unit for i in inputs if i.unit}
    if len(units) > 1:
        raise ValueError(
            f"refusing to combine mixed units {sorted(units)} — convert first; "
            "a sum across units is not a quantity"
        )
    return units.pop() if units else ""


def add(inputs: Iterable[Input]) -> Combination:
    """Sum. The operation every aggregate is built from."""
    items = list(inputs)
    if not items:
        return Combination(value=0.0, unit="", terms={}, low=0.0, high=0.0)
    unit = _unit_of(items)

    total = 0.0
    terms: dict[str, float] = {}
    loose: list[float] = []  # magnitudes without structure
    blind = 0
    for i in items:
        total += i.value
        if isinstance(i, Quantity):
            for s, a in i.terms.items():
                terms[s] = terms.get(s, 0.0) + a
        elif isinstance(i, MagnitudeOnly):
            loose.append(abs(i.u))
        else:
            blind += 1

    structured = math.hypot(*terms.values())
    # `high` is unconditional (triangle inequality in L²). `low` holds only
    # for non-negatively correlated loose terms; the complete floor allows
    # cancellation and is carried alongside.
    low = math.hypot(structured, *loose)
    magnitudes = ([structured] if structured else []) + list(loose)
    high = math.fsum(magnitudes)
    floor = max(0.0, 2 * max(magnitudes, default=0.0) - high)
    return Combination(
        value=total,
        unit=unit,
        terms=terms,
        low=low,
        high=high,
        low_unconstrained=floor,
        premise="exact" if not loose else "unstructured sources are non-negatively correlated",
        unquantified=blind,
        counted=len(items),
    )


def mean(inputs: Iterable[Input]) -> Combination:
    """Arithmetic mean — a sum scaled by 1/n, so correlation carries.

    This is the operation `gold_aggregate` performs today as plain ``avg``,
    dropping uncertainty entirely at the exact boundary a decision consumes
    it.
    """
    items = list(inputs)
    if not items:
        return Combination(value=0.0, unit="", terms={}, low=0.0, high=0.0)
    n = len(items)
    s = add(items)
    return Combination(
        value=s.value / n,
        unit=s.unit,
        terms={sym: a / n for sym, a in s.terms.items()},
        low=s.low / n,
        high=s.high / n,
        low_unconstrained=s.low_unconstrained / n,
        premise=s.premise,
        unquantified=s.unquantified,
        counted=s.counted,
    )


def difference(a: Quantity, b: Quantity) -> Quantity:
    """``a − b``, with shared sources cancelling as they physically do.

    Two sensors on one calibration bath differ more precisely than
    either is known absolutely, and this is where that shows up: the shared
    calibration term subtracts out. A scalar-only model cannot express it.
    """
    if a.unit and b.unit and a.unit != b.unit:
        raise ValueError(f"cannot subtract {b.unit} from {a.unit}")
    terms = dict(a.terms)
    for s, coeff in b.terms.items():
        terms[s] = terms.get(s, 0.0) - coeff
    return Quantity(value=a.value - b.value, unit=a.unit or b.unit, terms=terms)


def product(a: Quantity, b: Quantity, *, residual_symbol: str) -> Quantity:
    """``a × b``, linearised, with the residual tracked as its own source.

    Affine arithmetic's treatment of a nonlinear operation: the first-order
    part is exact, and what the linearisation threw away becomes a NEW named
    symbol rather than vanishing. An approximation that hides its own error
    is worse than no approximation.
    """
    check_symbol(residual_symbol)
    terms = {s: b.value * c for s, c in a.terms.items()}
    for s, c in b.terms.items():
        terms[s] = terms.get(s, 0.0) + a.value * c
    residual = a.u * b.u
    if residual:
        terms[residual_symbol] = terms.get(residual_symbol, 0.0) + residual
    unit = f"{a.unit}·{b.unit}" if a.unit and b.unit else (a.unit or b.unit)
    return Quantity(value=a.value * b.value, unit=unit, terms=terms)


@dataclass
class Registry:
    """Declared budgets, by symbol. Metadata only.

    Composition never consults this — it needs symbol NAMES and nothing
    else, so a map stays computable when the extension that produced it is
    absent. What the registry buys is EXPLANATION: what this uncertainty is
    about, what it is traceable to, and where it stops being valid.
    """

    _budgets: dict[str, Budget] = field(default_factory=dict)

    def declare(self, budget: Budget) -> None:
        prior = self._budgets.get(budget.symbol)
        if prior is not None and prior != budget:
            raise SymbolError(
                f"{budget.symbol!r} is already declared differently — a symbol whose "
                "meaning changed under it would silently rewrite every quantity "
                "that already referenced it"
            )
        self._budgets[budget.symbol] = budget

    def get(self, symbol: str) -> Budget | None:
        return self._budgets.get(symbol)

    def explain(self, q: Quantity | Combination) -> list[str]:
        """Which sources this quantity rests on, largest first, and what is
        undeclared. The undeclared ones are NAMED rather than skipped."""
        out = []
        for symbol, coeff in sorted(q.terms.items(), key=lambda kv: -abs(kv[1])):
            b = self.get(symbol)
            if b is None:
                out.append(f"{symbol}: contributes {abs(coeff):g}, but nothing declared it")
            else:
                out.append(
                    f"{symbol}: contributes {abs(coeff):g} — {b.measurand}, "
                    f"type {b.kind}, traceable to {b.traceable_to}, valid over {b.valid_over}"
                )
        return out

    def undeclared(self, q: Quantity | Combination) -> list[str]:
        return [s for s in q.terms if self.get(s) is None]


__all__ = [
    "TYPE_A",
    "TYPE_B",
    "Budget",
    "Combination",
    "Input",
    "MagnitudeOnly",
    "Quantity",
    "Registry",
    "SymbolError",
    "Unquantified",
    "add",
    "check_symbol",
    "correlation",
    "difference",
    "mean",
    "product",
]
