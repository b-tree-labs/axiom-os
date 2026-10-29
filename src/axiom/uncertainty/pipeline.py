# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Uncertainty through a simulate-train-predict pipeline, in stages.

Founder direction (2026-09-28): generalise so this handles ANY simulation,
training and prediction pipeline of a similar shape.

The shape, which recurs everywhere
----------------------------------
Something is measured. The measurements are conditioned into model inputs.
A high-fidelity model is solved. A cheaper surrogate is trained on those
solutions. The surrogate predicts. Sometimes the prediction is later
compared against what actually happened.

    observed → conditioned → high-fidelity solve → surrogate → prediction
                                                        ↕
                                              validated against observation

A reduced-order model trained on a physics code is this. So is a neural
surrogate trained on CFD, a data-driven twin of a process line, a yield
model fitted to simulated batches, and a demand forecast trained on a
simulated market. The stages below are named for the KIND of ignorance each
introduces, not for any domain, which is what makes one implementation
serve all of them.

Why stages rather than one number
---------------------------------
Because the stages fail differently and are reduced differently. Input
uncertainty shrinks by measuring better. Numerical uncertainty shrinks by
refining. Surrogate error shrinks by training harder. Model-form error
shrinks by changing the model, which is expensive and rare. And
extrapolation does not shrink at all — it means the number does not apply.

Collapsing them loses the one thing an engineer needs: WHERE to spend the
next hour. :func:`dominant` answers that, and it is the most practically
useful thing in this module.

Relationship to the standards
-----------------------------
This follows ASME V&V 20's decomposition, in which the comparison error
``E = S − D`` between simulation and data is explained by model form,
numerical and input errors plus the experimental uncertainty of the
reference, and the validation uncertainty ``u_val`` bounds how well the
model can be said to be known. GUM supplies the composition rules and the
Type A / Type B distinction; the affine form in this package supplies the
correlation bookkeeping that keeps stages composable at any depth.

The rule V&V 20 exists to enforce, and which this module makes structural:
**a prediction cannot be quoted tighter than the uncertainty of the
comparison that validated it.** :meth:`Pipeline.predict` refuses to.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from axiom.uncertainty import TYPE_A, TYPE_B, Budget, Quantity, check_symbol
from axiom.uncertainty.coverage import (
    DEFAULT_CONFIDENCE,
    INFINITE_DOF,
    coverage_factor,
    effective_dof,
)

#: Measurement uncertainty of what fed the model. Reduced by measuring
#: better. GUM Type A or B depending on how it was evaluated.
INPUT = "input"
#: Introduced by PREPARING data — resampling onto a common grid, unit
#: conversion, gap filling, alignment. Distinct from input uncertainty
#: because the instrument did not produce it; the pipeline did.
CONDITIONING = "conditioning"
#: The model's idealisation of reality: as-built geometry versus drawing,
#: physics left out, closure relations. Reduced only by changing the model.
MODEL_FORM = "model_form"
#: Discretisation, mesh, timestep, solver tolerance — V&V 20's u_num,
#: estimated by refinement (Richardson / grid convergence). Reduced by
#: refining, at known cost.
NUMERICAL = "numerical"
#: The surrogate's departure from the high-fidelity solutions it was
#: trained on: truncation rank, interpolation error, fit residual. Reduced
#: by more or better training.
SURROGATE = "surrogate"
#: Outside the envelope the surrogate was trained and validated over. This
#: one does NOT shrink and is not really a bound — it is a statement that
#: the prediction does not apply. Carried as a large term so it dominates
#: visibly rather than silently.
DOMAIN = "domain"
#: The model does not correspond to the system's current configuration —
#: it was built for a different one, or nobody recorded which. This is the
#: term that makes an apparent bias un-attributable: a disagreement may be
#: the model being wrong or the model being ASKED THE WRONG QUESTION, and
#: without this stage the two are indistinguishable.
CONFIGURATION = "configuration"
#: Empirically observed disagreement with reference measurement, after any
#: declared bias correction. Type A by construction — see :func:`from_parity`.
VALIDATION = "validation"

STAGES = (
    INPUT,
    CONDITIONING,
    MODEL_FORM,
    NUMERICAL,
    SURROGATE,
    DOMAIN,
    CONFIGURATION,
    VALIDATION,
)

#: Stages that more effort of the same kind will not reduce. Useful to say
#: out loud, because the usual instinct on seeing a wide interval is to
#: collect more data, and for these that is wasted work.
IRREDUCIBLE_BY_MORE_DATA = (MODEL_FORM, DOMAIN, CONFIGURATION)


@dataclass(frozen=True)
class Stage:
    """One named contribution, with the budget that explains it."""

    stage: str
    budget: Budget

    def __post_init__(self) -> None:
        if self.stage not in STAGES:
            raise ValueError(f"{self.stage!r} is not one of {STAGES}")

    @property
    def symbol(self) -> str:
        return self.budget.symbol

    @property
    def standard(self) -> float:
        return self.budget.standard


@dataclass(frozen=True)
class Parity:
    """What comparing a model against reality actually established.

    The most valuable uncertainty available to any pipeline, and usually
    the one already computed and thrown away: a bias, how well that bias
    itself is known, and what disagreement remains after removing it.
    """

    n: int
    bias: float
    #: Standard error of the bias — how well the CORRECTION is known. A
    #: correction applied as though exact imports an error of its own.
    bias_standard_error: float
    #: RMS of the residual after bias removal. This is the validation
    #: uncertainty: the model cannot be claimed better than this.
    residual_rms: float
    unit: str = ""

    @property
    def dof(self) -> float:
        """``n − 1``. A parity is Type A by construction, so its degrees of
        freedom are real and finite — which is exactly when the coverage
        factor stops being 2."""
        return float(self.n - 1)

    @property
    def gain(self) -> float:
        """Fraction of the raw disagreement the bias correction removes.

        Zero or negative means the correction is not worth applying, which
        is worth knowing before anybody ships one.
        """
        raw = math.hypot(self.bias, self.residual_rms)
        return 0.0 if raw == 0 else 1.0 - (self.residual_rms / raw)

    def reads(self) -> str:
        u = f" {self.unit}" if self.unit else ""
        return (
            f"over {self.n} paired comparisons the model ran {self.bias:g}{u} off, "
            f"±{self.bias_standard_error:g}{u} on that figure; removing it leaves "
            f"{self.residual_rms:g}{u} of disagreement that the bias does not explain"
        )


def from_parity(
    predicted: Sequence[float],
    observed: Sequence[float],
    *,
    unit: str = "",
) -> Parity:
    """Turn paired predictions and observations into what they establish.

    This is the step that is almost always missing. A parity comparison
    gets computed, a bias gets quoted, somebody corrects for it — and the
    residual scatter, which is the honest uncertainty of every future
    prediction, is left in a report.

    Both returned figures matter and they are not the same. The bias is a
    systematic offset you may correct for; its standard error says how well
    you know the correction. The residual RMS is what correcting cannot
    fix, and no prediction from this model may be quoted tighter.
    """
    if len(predicted) != len(observed):
        raise ValueError("parity needs paired samples: the sequences differ in length")
    n = len(predicted)
    if n < 2:
        raise ValueError(
            "a parity of fewer than two pairs establishes nothing — a single "
            "agreement is an anecdote and a single disagreement is a data point"
        )
    residuals = [p - o for p, o in zip(predicted, observed, strict=True)]
    # `statistics`, not hand-rolled and not numpy. fmean is fsum-based and so
    # correctly rounded, and `stdev` IS the residual RMS about the mean --
    # sqrt(sum((r - mean)^2) / (n - 1)) -- so computing it by hand was
    # reimplementing a proven primitive with worse accuracy. np.mean/np.std use
    # pairwise summation, which is faster and slightly less accurate here, and
    # these arrays are small.
    bias = statistics.fmean(residuals)
    rms = statistics.stdev(residuals)
    return Parity(
        n=n,
        bias=bias,
        bias_standard_error=rms / math.sqrt(n),
        residual_rms=rms,
        unit=unit,
    )


@dataclass(frozen=True)
class Pipeline:
    """A named prediction pipeline and everything known to make it uncertain."""

    name: str
    stages: tuple[Stage, ...] = ()

    def with_stage(self, stage: Stage) -> Pipeline:
        return Pipeline(name=self.name, stages=(*self.stages, stage))

    def by_stage(self) -> dict[str, float]:
        """Combined standard uncertainty per stage, independent within."""
        out: dict[str, float] = {}
        for s in self.stages:
            out[s.stage] = math.hypot(out.get(s.stage, 0.0), s.standard)
        return out

    def effective_dof(self) -> float:
        """Welch–Satterthwaite over the contributing stages (GUM Annex G).

        Computed over the RECONCILED contributions, so a validation stage
        contributes only its unexplained part — the same magnitudes the
        reported figure rests on, since a coverage factor derived from
        different magnitudes than the interval would not describe it.
        """
        gap = self.unexplained()
        contributions = [
            (gap if s.stage == VALIDATION else s.standard, s.budget.dof)
            for s in self.stages
            if s.stage != VALIDATION or gap > 0
        ]
        return effective_dof(contributions)

    def predict(
        self,
        value: float,
        *,
        unit: str,
        inputs: Quantity | None = None,
    ) -> Quantity:
        """The prediction, carrying every stage as a named source.

        ``inputs`` is the already-uncertain quantity the model consumed, so
        input uncertainty propagates by the same algebra as everything else
        and correlates correctly with anything sharing those sources — a
        prediction and its own input are not independent, and pipelines
        that treat them as such understate.
        """
        terms: dict[str, float] = dict(inputs.terms) if inputs is not None else {}
        # A validation stage is a CONSTRAINT, not a contributor. Adding it in
        # quadrature alongside the declared terms double counts, because the
        # measured disagreement already contains the numerical, input and
        # model-form error present at the compared conditions. What validation
        # legitimately adds is only the part the declared account does not
        # explain — see :meth:`unexplained`. With the account complete this
        # reproduces the measured figure exactly; with the account already
        # wider than the measurement, validation adds nothing and the account
        # stands, since a model may not be quoted better than its own account
        # either.
        gap = self.unexplained()
        for s in self.stages:
            if s.stage == VALIDATION:
                continue
            terms[s.symbol] = terms.get(s.symbol, 0.0) + s.standard
        if gap > 0:
            carrier = next(s.symbol for s in self.stages if s.stage == VALIDATION)
            terms[carrier] = terms.get(carrier, 0.0) + gap
        return Quantity(value=value, unit=unit, terms=terms)

    def dominant(self, *, top: int = 3) -> list[tuple[str, str, float, float]]:
        """``(stage, symbol, standard, share of variance)``, largest first.

        The practical payoff. Variance share rather than magnitude, because
        uncertainties add in quadrature and a term at half the size of
        another contributes a quarter as much — which is the difference
        between a worthwhile afternoon and a wasted one.
        """
        # Ranked on RECONCILED contributions, so the shares here are the same
        # shares the budget prints. A validation stage contributes only the
        # part the declared account does not explain — see :meth:`predict`.
        gap = self.unexplained()
        contributions = [
            (s.stage, s.symbol, gap if s.stage == VALIDATION else s.standard)
            for s in self.stages
            if s.stage != VALIDATION or gap > 0
        ]
        total = math.fsum(u * u for _, _, u in contributions)
        if total <= 0:
            return []
        ranked = sorted(contributions, key=lambda c: -c[2])
        return [(st, sym, u, u * u / total) for st, sym, u in ranked[:top]]

    def unexplained(self) -> float:
        """How much observed disagreement the declared terms do NOT account for.

        V&V 20's actual diagnostic, and the most valuable number here.
        Validation uncertainty is a MEASUREMENT of the model's total error.
        The other stages are an ACCOUNT of where that error comes from. When
        the measurement exceeds the account, the account is incomplete —
        there is a real error source nobody has written down.

        Returned in quadrature, so it is directly comparable to the
        declared terms: ``sqrt(u_val² − Σu_declared²)``, or zero when the
        account already covers the measurement.
        """
        val = math.fsum(s.standard**2 for s in self.stages if s.stage == VALIDATION)
        declared = math.fsum(s.standard**2 for s in self.stages if s.stage != VALIDATION)
        return math.sqrt(val - declared) if val > declared else 0.0

    def advice(self) -> str:
        """One sentence on where the next hour goes, or that it is wasted."""
        ranked = self.dominant(top=1)
        if not ranked:
            return f"{self.name}: nothing has been declared, so nothing can be said."

        # An incomplete account leads, whatever its rank. It is a different
        # CLASS of finding from a large-but-understood term: the model
        # disagrees with reality by more than its own explanation of itself
        # covers, so a real error source is missing from the budget.
        # Refining the largest declared term is the tempting move and the
        # wrong one, because every declared term is defensible and one of
        # them is always the largest.
        gap = self.unexplained()
        if gap > 0:
            biggest = max(
                (s for s in self.stages if s.stage != VALIDATION),
                key=lambda s: s.standard,
                default=None,
            )
            tail = (
                f" The largest term that IS declared is {biggest.symbol} at "
                f"{biggest.standard:g}, and refining it cannot close this."
                if biggest is not None
                else ""
            )
            return (
                f"{self.name}: the declared terms explain less than the comparison "
                f"measured — {gap:.3g} of disagreement is unaccounted for, which is a "
                f"real error source nobody has written down. Find the missing term "
                f"first.{tail}"
            )

        stage, symbol, standard, share = ranked[0]
        head = (
            f"{self.name}: {stage} dominates — {symbol} at {standard:g} "
            f"is {share:.0%} of the variance"
        )
        if stage in IRREDUCIBLE_BY_MORE_DATA:
            return (
                f"{head}. More of the same data will not reduce it; this needs a "
                "different model, a matched configuration, or staying inside the "
                "envelope."
            )
        return f"{head}. That is where refinement pays."


def validation_stage(
    parity: Parity,
    *,
    symbol: str,
    measurand: str,
    valid_over: str = "unstated",
) -> Stage:
    """The validation stage, evaluated from observation — GUM Type A.

    Its ``valid_over`` matters more than most: a validation establishes the
    model over the conditions the comparison actually covered, and says
    nothing whatever outside them.
    """
    check_symbol(symbol)
    return Stage(
        stage=VALIDATION,
        budget=Budget(
            symbol=symbol,
            measurand=measurand,
            standard=parity.residual_rms,
            kind=TYPE_A,
            traceable_to=f"{parity.n} paired comparisons against reference measurement",
            valid_over=valid_over,
            note=parity.reads(),
            dof=parity.dof,
        ),
    )


def correction_stage(
    parity: Parity,
    *,
    symbol: str,
    measurand: str,
) -> Stage:
    """Applying a measured bias imports the uncertainty OF that bias.

    A correction quoted as though exact is a systematic error wearing a fix,
    and this is the term people forget: the residual gets carried, the
    standard error of the correction does not.
    """
    check_symbol(symbol)
    return Stage(
        stage=INPUT,
        budget=Budget(
            symbol=symbol,
            measurand=f"bias correction applied to {measurand}",
            standard=parity.bias_standard_error,
            kind=TYPE_A,
            traceable_to=f"standard error of the mean over {parity.n} comparisons",
            valid_over="the conditions the comparison covered",
            dof=parity.dof,
        ),
    )


def stage(
    kind: str,
    *,
    symbol: str,
    measurand: str,
    standard: float,
    evaluated: str = TYPE_B,
    traceable_to: str = "unstated",
    valid_over: str = "unstated",
    note: str = "",
    dof: float = INFINITE_DOF,
) -> Stage:
    """Declare one stage. The call a contributing extension makes.

    ``dof`` defaults to unlimited, the GUM convention for a Type B bound. A
    stage evaluated from ``n`` observations should pass ``n − 1``, because
    that is what makes the reported interval honest at small ``n``.
    """
    return Stage(
        stage=kind,
        budget=Budget(
            symbol=symbol,
            measurand=measurand,
            standard=standard,
            kind=evaluated,
            traceable_to=traceable_to,
            valid_over=valid_over,
            note=note,
            dof=dof,
        ),
    )


__all__ = [
    "CONDITIONING",
    "CONFIGURATION",
    "DOMAIN",
    "INPUT",
    "IRREDUCIBLE_BY_MORE_DATA",
    "MODEL_FORM",
    "NUMERICAL",
    "STAGES",
    "SURROGATE",
    "VALIDATION",
    "Parity",
    "Pipeline",
    "Stage",
    "correction_stage",
    "from_parity",
    "render_budget",
    "stage",
    "validation_stage",
]


def render_budget(
    pipeline: Pipeline,
    *,
    value: float,
    unit: str,
    confidence: float = DEFAULT_CONFIDENCE,
) -> str:
    """The uncertainty budget, as GUM §7 requires it to be reported.

    Not a convenience. A combined uncertainty published without the budget
    that produced it cannot be checked, cannot be reproduced, and cannot be
    argued with — which is why the standard asks for the table and not the
    number. It is also the only form in which the figure is useful to
    somebody deciding what to fix.

    Every row names its measurand, how it was evaluated, what it traces to,
    and over what it is valid, because a term missing any of those is a
    number somebody will later have to take on faith.
    """
    q = pipeline.predict(value, unit=unit)
    total_var = math.fsum(s.standard**2 for s in pipeline.stages)
    lines = [
        f"Uncertainty budget — {pipeline.name}",
        f"  result: {value:g} {unit}",
        "",
    ]
    if not pipeline.stages:
        lines.append("  Nothing has been declared. This result carries no stated uncertainty,")
        lines.append("  which is not the same as being exact.")
        return "\n".join(lines)

    width = max(len(s.symbol) for s in pipeline.stages)
    contributors = [s for s in pipeline.stages if s.stage != VALIDATION]
    constraints = [s for s in pipeline.stages if s.stage == VALIDATION]
    gap = pipeline.unexplained()
    total_var = math.fsum(s.standard**2 for s in contributors) + gap**2

    lines.append(f"  {'source':<{width}}  {'stage':<13} {'u':>10}  {'var%':>5}  ev")
    lines.append(f"  {'-' * width}  {'-' * 13} {'-' * 10}  {'-' * 5}  --")
    for s in sorted(contributors, key=lambda x: -x.standard):
        share = s.standard**2 / total_var if total_var else 0.0
        lines.append(
            f"  {s.symbol:<{width}}  {s.stage:<13} {s.standard:>10.4g}  "
            f"{share:>4.0%}  {s.budget.kind}"
        )
    if gap > 0:
        share = gap**2 / total_var if total_var else 0.0
        carrier = constraints[0].symbol
        lines.append(f"  {carrier:<{width}}  {'unexplained':<13} {gap:>10.4g}  {share:>4.0%}  A")
    lines.append("")
    if constraints:
        # Shown separately and deliberately NOT summed with the rows above.
        # Quoting it as one more contribution is the arithmetic this module
        # exists to prevent: the comparison already measured the total.
        for c in constraints:
            lines.append(
                f"  measured against reference: {c.standard:.4g} {unit} over "
                f"{c.budget.traceable_to}"
            )
            lines.append(f"    valid over {c.budget.valid_over}")
        lines.append(
            "  That is a constraint on the total, not another term — the rows above "
            "are reconciled to it."
        )
        lines.append("")
    dof = pipeline.effective_dof()
    k = coverage_factor(confidence=confidence, dof=dof)
    low, high, _ = q.coverage(confidence=confidence, dof=dof)
    dof_text = "effectively unlimited" if not math.isfinite(dof) else f"{dof:.4g}"
    lines.append(f"  combined standard uncertainty  u  = {q.u:.4g} {unit}")
    # k DERIVED from the effective degrees of freedom, not the habitual 2.
    # Welch-Satterthwaite (GUM Annex G) over the same reconciled magnitudes
    # the interval rests on.
    lines.append(f"  effective degrees of freedom        {dof_text}")
    lines.append(f"  coverage factor at {confidence:.0%}            k  = {k:.4g}")
    lines.append(f"  expanded uncertainty               U  = {k * q.u:.4g} {unit}")
    lines.append(f"  coverage interval                   {low:.4g} to {high:.4g} {unit}")
    lines.append("")
    lines.append(f"  {pipeline.advice()}")

    unstated = [s for s in pipeline.stages if s.budget.valid_over == "unstated"]
    if unstated:
        lines.append("")
        lines.append(
            f"  {len(unstated)} term(s) do not state what they are valid over, so this "
            "budget's own range of applicability is unknown:"
        )
        for s in unstated:
            lines.append(f"    - {s.symbol}")
    return "\n".join(lines)
