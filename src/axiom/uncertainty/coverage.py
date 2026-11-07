# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs

# SPDX-License-Identifier: Apache-2.0

"""Coverage factors, from the t-distribution rather than from habit.

Why ``k = 2`` is not good enough
--------------------------------
An expanded uncertainty is meaningless without the coverage factor and the
confidence it corresponds to — GUM says so, and ``k = 2`` is the number
everybody writes. It is ~95% only in the limit of infinite degrees of
freedom.

A Type A component evaluated from ``n`` observations has ``n − 1`` degrees
of freedom, and the correct 95% factor grows sharply as that falls:

=====  ==========
dof    k at 95%
=====  ==========
1      12.71
5      2.57
10     2.23
160    1.97
∞      1.96
=====  ==========

At six observations, quoting ``k = 2`` understates the interval by 29%. On
a chart that is invisible. In a limit check it is the difference between
compliant and not.

Effective degrees of freedom
----------------------------
A combined uncertainty mixes components with different degrees of freedom,
so the interval needs a single effective value. GUM Annex G gives the
Welch–Satterthwaite formula:

.. math::

    \\nu_{eff} = \\frac{u_c^4}{\\sum_i u_i^4 / \\nu_i}

A component with infinite degrees of freedom contributes nothing to the
denominator, which is the right behaviour: a Type B bound from a
certificate does not make the interval wider through this route.

The consequence worth knowing is that ``ν_eff`` is dominated by whichever
LARGE component is worst-determined. One poorly-estimated term can pull the
whole interval wide even when the rest are solid — which is information,
and is exactly what :func:`effective_dof` exists to surface.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

#: A component known well enough that its degrees of freedom do not bind.
#: The GUM convention for a Type B bound from a certificate or a tolerance.
INFINITE_DOF = math.inf

#: The conventional reporting level. Stated rather than assumed, because a
#: coverage factor without its confidence is not a fact.
DEFAULT_CONFIDENCE = 0.95


def effective_dof(contributions: Sequence[tuple[float, float]]) -> float:
    """Welch–Satterthwaite effective degrees of freedom (GUM Annex G).

    ``contributions`` is ``(standard uncertainty, degrees of freedom)`` per
    component. Components with infinite or non-positive degrees of freedom
    are skipped in the denominator — infinite because it contributes zero,
    non-positive because a component with no degrees of freedom carries no
    information about its own spread and cannot be allowed to drive the
    interval to nonsense.

    Returns ``INFINITE_DOF`` when nothing constrains it, which is honest:
    with every component well-determined there is no small-sample penalty.
    """
    magnitudes = [u for u, _ in contributions if u]
    if not magnitudes:
        return INFINITE_DOF
    combined = math.hypot(*magnitudes)
    if combined <= 0:
        return INFINITE_DOF
    denominator = math.fsum(
        (u**4) / dof for u, dof in contributions if u and math.isfinite(dof) and dof > 0
    )
    if denominator <= 0:
        return INFINITE_DOF
    return (combined**4) / denominator


def coverage_factor(*, confidence: float = DEFAULT_CONFIDENCE, dof: float = INFINITE_DOF) -> float:
    """The two-sided coverage factor ``k`` for a confidence and dof.

    ``scipy.stats.t.ppf``, which handles ``dof = inf`` by converging on the
    normal quantile — so the infinite case needs no special branch and no
    second code path to keep consistent.
    """
    if not 0.0 < confidence < 1.0:
        raise ValueError(
            f"confidence must be strictly between 0 and 1, got {confidence!r}; "
            "a 100% coverage interval is unbounded and a 0% one is empty"
        )
    if dof <= 0:
        raise ValueError(
            f"degrees of freedom must be positive, got {dof!r}; a component "
            "with none says nothing about its own spread, so no interval "
            "follows from it"
        )
    from scipy import stats

    return float(stats.t.ppf(1.0 - (1.0 - confidence) / 2.0, dof))


def interval(
    value: float,
    standard: float,
    *,
    confidence: float = DEFAULT_CONFIDENCE,
    dof: float = INFINITE_DOF,
) -> tuple[float, float, float]:
    """``(low, high, k)`` — the coverage interval and the factor used.

    ``k`` is returned rather than left implicit because an interval whose
    coverage factor a reader has to guess is not reportable, and because the
    factor is the part that changes when the degrees of freedom do.
    """
    k = coverage_factor(confidence=confidence, dof=dof)
    return (value - k * standard, value + k * standard, k)


def reads(
    *, standard: float, confidence: float = DEFAULT_CONFIDENCE, dof: float = INFINITE_DOF
) -> str:
    """The sentence GUM asks for: the figure, the factor, and the level."""
    k = coverage_factor(confidence=confidence, dof=dof)
    dof_text = "effectively unlimited" if not math.isfinite(dof) else f"{dof:.3g}"
    return (
        f"± {k * standard:.4g} at {confidence:.0%} confidence "
        f"(k = {k:.4g}, {dof_text} degrees of freedom)"
    )


__all__ = [
    "DEFAULT_CONFIDENCE",
    "INFINITE_DOF",
    "coverage_factor",
    "effective_dof",
    "interval",
    "reads",
]
