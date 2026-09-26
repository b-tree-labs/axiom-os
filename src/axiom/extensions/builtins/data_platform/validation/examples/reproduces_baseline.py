# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Regression against a previous revision — the SAME shape, a different reference.

The question: did refactoring this model change what it produces? That is not
the same question as whether the model is right, and conflating them is a real
risk on this program — a refactor can preserve behaviour perfectly while the
behaviour remains wrong, and it can also improve accuracy while failing a strict
regression test.

So this is a separate check, registered alongside the validation one, failing
for its own reasons. Note how little differs from ``tracks_reference``: the
reference comes from ``subject.context`` instead of ``subject.measured``, and
the tolerance is tighter because a refactor is expected to reproduce, not merely
approximate.
"""

from __future__ import annotations

from axiom.extensions.builtins.data_platform.validation import (
    Outcome,
    Subject,
    Verdict,
    paired,
    within_tolerance,
)

CHECK_NAME = "reproduces-baseline"

#: A refactor should reproduce its baseline, so this is far tighter than a
#: validation bound. It is not zero, because floating-point reassociation across
#: a refactor is expected and is not a behaviour change.
MAX_ABSOLUTE = 0.01
MAX_RELATIVE = 1e-6


def reproduces_baseline(subject: Subject) -> Verdict:
    """Candidate output must reproduce the baseline revision's output.

    The baseline arrives in ``subject.context["baseline"]`` as rows in the same
    shape as ``subject.predicted``. Configuration, not code: which revision is
    the baseline is a deployment's decision, not this file's.
    """
    baseline = subject.context.get("baseline")
    if not baseline:
        # No baseline is not a pass. A regression check with nothing to compare
        # against has not verified anything, and reporting it green would make
        # "we lost the baseline" indistinguishable from "nothing changed".
        return Verdict(
            check=CHECK_NAME,
            model_ref=subject.model_ref,
            outcome=Outcome.ERROR,
            detail="no baseline supplied in context; nothing to compare against",
        )

    rows = paired(subject.predicted, baseline, key="ts", value="reading")
    if not rows:
        return Verdict(
            check=CHECK_NAME,
            model_ref=subject.model_ref,
            outcome=Outcome.ERROR,
            detail="candidate and baseline share no timestamps",
        )

    drifted = [
        (ts, cand, base)
        for ts, cand, base in rows
        if not within_tolerance(cand, base, absolute=MAX_ABSOLUTE, relative=MAX_RELATIVE)
    ]

    if not drifted:
        return Verdict(
            check=CHECK_NAME,
            model_ref=subject.model_ref,
            outcome=Outcome.PASS,
            observed=0.0,
            threshold=MAX_ABSOLUTE,
            detail=f"{len(rows)} states reproduce the baseline",
        )

    worst_ts, cand, base = max(drifted, key=lambda r: abs(r[1] - r[2]))
    return Verdict(
        check=CHECK_NAME,
        model_ref=subject.model_ref,
        outcome=Outcome.FAIL,
        observed=abs(cand - base),
        threshold=MAX_ABSOLUTE,
        detail=(
            f"{len(drifted)} of {len(rows)} states drifted; worst at {worst_ts}: "
            f"{cand} against baseline {base}"
        ),
        evidence={"worst_ts": worst_ts, "drifted_count": len(drifted)},
    )
