# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Validation and QC at the silver → gold promotion, as a plug-in point.

The sibling of :mod:`..conformance`, and deliberately the same shape. Where
conformance dispatches on ``schema_ref`` to a normalizer that reshapes a row,
this dispatches on ``model_ref`` to a check that judges one. The placement rule
is the same and is the reason both live here:

    these mechanics are domain-agnostic and live here; domain checks live in
    downstream platform extensions; deployments contribute configuration, never
    pipeline python.

Promotion is the platform's. What counts as *valid* is not. A surrogate
model's error bounds against measurement, the threshold that makes it fit to
serve, the tolerance a regression must stay inside: those are judgements made by
whoever owns the physics, and they change on a different clock from the
pipeline. So the pipeline carries a registry and the judgement arrives as a
registered function.


Why a verdict is not a boolean
------------------------------

A check has three outcomes, not two, and collapsing the third is how a QC
system starts lying.

``PASS`` and ``FAIL`` are judgements: the check ran and reached a conclusion.
``ERROR`` is not a judgement. It means the check could not run at all, because
the reference data was missing, a query failed, or the model was unavailable.
``SKIP`` means the check does not apply to this input and never intended to.

Folding ``ERROR`` into ``FAIL`` makes a QC score drop when infrastructure
degrades, and a score that falls when the network does is measuring the network.
Folding it into ``PASS`` is worse. So :class:`Summary` counts passes and
failures, reports errors and skips separately, and refuses to produce a pass
rate when it cannot say what it ran.

This is not a hypothetical. Four checks in this portfolio have been found unable
to fail: a mass density pinned by ``set_density`` over a composition that was
wrong by a factor of two, a material source defaulted to ``lambda: []``, a
migration reporting one extension's revisions as the whole database, and a
decimation gate keeping every frame it saw. Each one ran, each one was green,
and none of them could have gone red.


Tolerances are absolute *and* relative
--------------------------------------

:func:`within_tolerance` takes both, and a check that only sets one is usually
wrong.

A relative-only comparison is meaningless near zero and is defeated by
quantisation: a signal recorded to the nearest degree, sitting at fifteen
degrees, moves 6.7% when it moves one count. A gate written that way keeps every
frame and reports itself healthy. An absolute-only comparison, conversely, is
useless across the range of a signal that spans decades.

So both, and a value passes when it is inside *either*.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "Check",
    "CheckRegistry",
    "Outcome",
    "Subject",
    "Summary",
    "Verdict",
    "run_checks",
    "within_tolerance",
]


class Outcome(str, Enum):
    """What happened when a check ran.

    ``PASS`` and ``FAIL`` are judgements. ``ERROR`` and ``SKIP`` are not, and
    are never aggregated with them.
    """

    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIP = "skip"

    @property
    def is_judgement(self) -> bool:
        return self in (Outcome.PASS, Outcome.FAIL)


@dataclass(frozen=True)
class Subject:
    """What a check is asked to judge.

    Args:
        model_ref: Identifies the model under test, e.g.
            ``model:example-surrogate@3``. This is the registry key, so a check
            registered for one revision does not silently judge another.
        predicted: Rows the model produced.
        measured: Rows to judge them against. Empty is a legitimate state and
            must produce ``ERROR``, not ``FAIL`` — a model is not wrong because
            nobody measured anything.
        context: Anything else the check needs. Configuration, not code.
    """

    model_ref: str
    predicted: tuple[Mapping[str, Any], ...] = ()
    measured: tuple[Mapping[str, Any], ...] = ()
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    """One check's conclusion about one subject.

    ``observed`` and ``threshold`` are carried so a reader can see how close a
    pass was, which a boolean cannot express. A verdict that barely passed and
    one that passed by three orders of magnitude are different facts about a
    model, and only one of them is reassuring.
    """

    check: str
    model_ref: str
    outcome: Outcome
    detail: str = ""
    observed: float | None = None
    threshold: float | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True only for an actual pass.

        Never true for ``ERROR``. Reading a verdict as ``if v.ok`` is safe;
        reading it as ``if not v.failed`` is the mistake this property exists to
        prevent.
        """
        return self.outcome is Outcome.PASS


#: A check is a pure function of a subject. No class to inherit, matching the
#: ``Normalizer`` contract next door: the thing a contributor writes should be
#: readable in one screen and testable without the platform running.
Check = Callable[[Subject], Verdict]


class CheckRegistry:
    """model_ref → checks. Mirrors :class:`..conformance.NormalizerRegistry`.

    Unlike normalizers, several checks may register against one key: a model can
    be judged on more than one axis, and the reason to keep them separate is
    that they fail for different reasons and are owned by different people.
    """

    def __init__(self) -> None:
        self._by_ref: dict[str, dict[str, Check]] = {}

    def register(self, model_ref: str, name: str, fn: Check) -> None:
        """Register ``fn`` as check ``name`` for ``model_ref``.

        Raises:
            ValueError: If that name is already registered for that model_ref.
                Silently replacing a check would mean a deployment could lose a
                QC gate by installing a package, which is precisely the failure
                a registry is supposed to make impossible.
        """
        existing = self._by_ref.setdefault(model_ref, {})
        if name in existing:
            raise ValueError(f"check {name!r} already registered for {model_ref!r}")
        existing[name] = fn

    def get(self, model_ref: str) -> dict[str, Check]:
        return dict(self._by_ref.get(model_ref, {}))

    def refs(self) -> list[str]:
        return sorted(self._by_ref)

    def names(self, model_ref: str) -> list[str]:
        return sorted(self._by_ref.get(model_ref, {}))


@dataclass(frozen=True)
class Summary:
    """The result of running every check registered for a subject.

    Deliberately awkward to misuse. There is no single number on this class
    that folds errors into failures, because the whole point is that they are
    different.
    """

    model_ref: str
    verdicts: tuple[Verdict, ...]

    @property
    def judged(self) -> tuple[Verdict, ...]:
        return tuple(v for v in self.verdicts if v.outcome.is_judgement)

    @property
    def passed(self) -> tuple[Verdict, ...]:
        return tuple(v for v in self.verdicts if v.outcome is Outcome.PASS)

    @property
    def failed(self) -> tuple[Verdict, ...]:
        return tuple(v for v in self.verdicts if v.outcome is Outcome.FAIL)

    @property
    def errored(self) -> tuple[Verdict, ...]:
        return tuple(v for v in self.verdicts if v.outcome is Outcome.ERROR)

    @property
    def skipped(self) -> tuple[Verdict, ...]:
        return tuple(v for v in self.verdicts if v.outcome is Outcome.SKIP)

    @property
    def pass_rate(self) -> float | None:
        """Passes over judgements, or None when nothing was judged.

        ``None`` rather than ``0.0`` or ``1.0``. A run where every check errored
        has no pass rate, and inventing one would report an outage as a result.
        """
        if not self.judged:
            return None
        return len(self.passed) / len(self.judged)

    @property
    def promotable(self) -> bool:
        """Whether this subject may be promoted to gold.

        Requires at least one judgement, no failures, and no errors. Errors
        block on purpose: "we could not check" is not permission to proceed,
        and a promotion gate that opens when its checks cannot run is not a
        gate.
        """
        return bool(self.judged) and not self.failed and not self.errored

    def reasons(self) -> list[str]:
        """Human-readable reasons this subject is not promotable."""
        out = [f"{v.check}: {v.detail or 'failed'}" for v in self.failed]
        out += [f"{v.check}: could not run — {v.detail or 'no detail'}" for v in self.errored]
        if not self.judged:
            out.append("no check reached a judgement")
        return out


def run_checks(subject: Subject, registry: CheckRegistry) -> Summary:
    """Run every check registered for ``subject.model_ref``.

    A check that raises becomes an ``ERROR`` verdict rather than propagating.
    One contributor's bug must not take down a promotion run or hide the
    verdicts of the checks that did work, which is the same rule discovery
    applies to registration.
    """
    verdicts: list[Verdict] = []
    for name, fn in sorted(registry.get(subject.model_ref).items()):
        try:
            verdict = fn(subject)
        except Exception as exc:  # noqa: BLE001 — a bad check is an error, not a failure
            log.warning("check %r for %r raised: %s", name, subject.model_ref, exc)
            verdicts.append(
                Verdict(
                    check=name,
                    model_ref=subject.model_ref,
                    outcome=Outcome.ERROR,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        if verdict is None:
            verdicts.append(
                Verdict(
                    check=name,
                    model_ref=subject.model_ref,
                    outcome=Outcome.ERROR,
                    detail="check returned no verdict",
                )
            )
            continue
        # Stamp the registered name over whatever the check called itself.
        #
        # A check knows what it does; the registry knows what it *is* here. If a
        # verdict could report a name that does not appear in the registry, a
        # promotion report would name a failing check that nobody can look up,
        # and two packages could both report as "bounds" while registered
        # separately. The registry key is the identity, so it wins.
        verdicts.append(
            verdict
            if verdict.check == name
            else replace(verdict, check=name, model_ref=subject.model_ref)
        )
    return Summary(model_ref=subject.model_ref, verdicts=tuple(verdicts))


def within_tolerance(
    observed: float,
    reference: float,
    *,
    absolute: float,
    relative: float,
) -> bool:
    """Whether ``observed`` agrees with ``reference`` within either tolerance.

    Both are required, and passing zero for one of them is a decision rather
    than a default.

    A relative-only test is defeated by quantisation and meaningless near zero:
    a signal recorded to the nearest degree at fifteen degrees moves 6.7% when
    it moves one count, so a relative gate at 5% treats instrument resolution as
    real change. An absolute-only test cannot follow a signal that spans orders
    of magnitude.

    Non-finite values return False. A NaN is not agreement, and letting one pass
    would put a silent hole in whatever it was checking.
    """
    if not (math.isfinite(observed) and math.isfinite(reference)):
        return False
    difference = abs(observed - reference)
    if difference <= absolute:
        return True
    if reference == 0:
        return False
    return difference / abs(reference) <= relative


def paired(
    predicted: Iterable[Mapping[str, Any]],
    measured: Iterable[Mapping[str, Any]],
    *,
    key: str,
    value: str,
) -> list[tuple[Any, float, float]]:
    """Pair predicted and measured rows on ``key``, returning ``(key, pred, meas)``.

    Only pairs present on both sides are returned. A predicted row with no
    measurement is not a disagreement, and a check that treated it as one would
    penalise a model for the sampling schedule of the instrument.
    """
    by_key = {row[key]: row for row in measured if key in row and value in row}
    out: list[tuple[Any, float, float]] = []
    for row in predicted:
        if key not in row or value not in row:
            continue
        match = by_key.get(row[key])
        if match is None:
            continue
        out.append((row[key], float(row[value]), float(match[value])))
    return out
