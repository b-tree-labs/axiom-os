# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Validation against measurement — a WORKING example you can copy.

The question: does this surrogate model track the instrument closely enough
to be trusted? That is what a validation record asserts, and the numbers below
are where the assertion actually lives.

Copy this file and change three things: the metric, the two tolerances, and the
model_ref you register against. Everything else is the shape.
"""

from __future__ import annotations

from axiom.extensions.builtins.data_platform.validation import (
    Outcome,
    Subject,
    Verdict,
    paired,
    within_tolerance,
)

CHECK_NAME = "tracks-reference"

#: Absolute tolerance, in engineering units.
#:
#: The instrument records to the nearest degree, so anything below about three
#: counts is measuring quantisation rather than the model. Five is chosen to sit
#: clearly above that, not because five is a physically meaningful number.
MAX_ABSOLUTE = 5.0

#: Relative tolerance, once the signal is well above the noise floor.
#:
#: Both are set on purpose. A relative-only gate is defeated by quantisation at
#: low readings; an absolute-only gate cannot follow the signal across its range.
MAX_RELATIVE = 0.02


def tracks_reference(subject: Subject) -> Verdict:
    """Predicted values must track the reference within bounds."""
    rows = paired(subject.predicted, subject.measured, key="ts", value="reading")

    if not rows:
        # ERROR, not FAIL. The model is not wrong because the instrument was not
        # sampling. Calling this a failure would make the QC number track the
        # measurement schedule instead of the physics, and it would drop during
        # exactly the outages when someone is most likely to be reading it.
        return Verdict(
            check=CHECK_NAME,
            model_ref=subject.model_ref,
            outcome=Outcome.ERROR,
            detail="no paired predicted/measured rows in the window",
        )

    deviations = [(ts, abs(pred - meas)) for ts, pred, meas in rows]
    worst_ts, worst_dev = max(deviations, key=lambda pair: pair[1])
    outside = [
        ts
        for ts, pred, meas in rows
        if not within_tolerance(pred, meas, absolute=MAX_ABSOLUTE, relative=MAX_RELATIVE)
    ]

    return Verdict(
        check=CHECK_NAME,
        model_ref=subject.model_ref,
        outcome=Outcome.FAIL if outside else Outcome.PASS,
        observed=worst_dev,
        threshold=MAX_ABSOLUTE,
        detail=(
            f"{len(rows)} paired points, {len(outside)} outside bounds, "
            f"worst deviation {worst_dev:.4g} at {worst_ts}"
        ),
        evidence={"worst_ts": worst_ts, "outside_count": len(outside)},
    )
