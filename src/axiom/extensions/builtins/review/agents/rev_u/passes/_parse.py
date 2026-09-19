# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One parser for every review pass, and one way to report a pass that failed.

This was copy-pasted into all five passes — byte-identical in four of them —
and every copy collapsed three unrelated outcomes into an empty list: the model
found nothing, the model's answer did not parse, and the model was never
reached. Only the first of those is a clean review.

Parsing now refuses to guess, and a pass that cannot complete emits a Finding
saying so. That follows the precedent already set by `gather_context`, which
returns its warnings as Findings so a degraded run is visible to whoever reads
the review rather than buried in a log line nobody tails.
"""

from __future__ import annotations

import json

from axiom.extensions.builtins.review.tools.findings import Finding


class PassOutputUnreadable(RuntimeError):
    """The model answered, but not with findings we can read."""


def parse_findings(text: str, *, pass_kind: str) -> list[Finding]:
    """Parse a pass response, or raise rather than report a false all-clear.

    An empty list is a real answer and stays one. Anything we cannot turn into
    findings raises, because the caller needs to tell the difference.
    """
    try:
        raw = json.loads((text or "").strip())
    except (json.JSONDecodeError, AttributeError) as exc:
        raise PassOutputUnreadable(
            f"{pass_kind}: response was not JSON ({exc})"
        ) from exc

    if not isinstance(raw, list):
        raise PassOutputUnreadable(
            f"{pass_kind}: expected a list of findings, got {type(raw).__name__}"
        )

    findings: list[Finding] = []
    for item in raw:
        if not isinstance(item, dict):
            raise PassOutputUnreadable(
                f"{pass_kind}: expected finding objects, got {type(item).__name__}"
            )
        findings.append(
            Finding(
                severity=item.get("severity", "major"),
                pass_kind=pass_kind,
                path=item.get("path", ""),
                line=item.get("line"),
                message=item.get("message", ""),
                suggested_fix=item.get("suggested_fix"),
            )
        )
    return findings


def degraded(pass_kind: str, reason: str) -> Finding:
    """A Finding that says this pass did not run, so nobody reads it as clean.

    `severity="minor"` matches what `gather_context` uses for the same purpose:
    high enough to appear in the review, not so high it outranks real defects
    found by the passes that did work.
    """
    return Finding(
        severity="minor",
        pass_kind=pass_kind,
        path="",
        line=None,
        message=(
            f"The {pass_kind} pass did not complete, so this review says nothing "
            f"about {pass_kind}. Treat it as unreviewed, not as clean. Cause: {reason}"
        ),
        suggested_fix="Re-run the review once the cause is resolved.",
    )


__all__ = ["PassOutputUnreadable", "degraded", "parse_findings"]
