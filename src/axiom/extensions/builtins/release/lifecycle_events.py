# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET lifecycle-event emission — the signal half of ADR-046.

RIVET is the authoritative signal of merge/ship state. It *emits* lifecycle
events on the default EventBus; TIDY (and any other consumer) subscribes to
drive its own work — e.g. TIDY reclaims a merged branch on `rivet.pr_merged`.

Per ADR-046, RIVET makes the green and only signals; it performs no
destructive git operations. Emission is **best-effort**: signalling is
advisory, so a missing or failing bus never breaks RIVET's primary flow
(notifying, closing its CI-failure issues, cutting a release).
"""

from __future__ import annotations

from typing import Any

# Lifecycle event subjects (consumed by TIDY's branch-hygiene + others).
PR_MERGED = "rivet.pr_merged"
TAG_RELEASED = "rivet.tag_released"
CI_RECOVERED = "rivet.ci_recovered"

# The subjects that are *about a branch*, and so can name the lane it was on.
# TIDY would otherwise have to infer the lane from a slug — the one inference
# lane_signal exists to prevent — so RIVET attaches the answer here (ADR-046:
# the two halves meet only at the event bus).
_BRANCH_SUBJECTS = frozenset({PR_MERGED, TAG_RELEASED})


def _with_lane(subject: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Attach the lane a branch was on, best-effort. Never raises.

    Reads the lane registry and lets ``lane_signal.annotate`` decide: an event
    whose branch names no lane comes back unchanged (no empty keys), so a
    consumer still distinguishes "no lane" from "lane unknown". A missing lane
    extension or an unreadable registry is simply "no lane" — signalling is
    advisory and must not break the emit.
    """
    if subject not in _BRANCH_SUBJECTS or not payload.get("branch"):
        return payload
    try:
        from axiom.extensions.builtins.lane.registry import Registry
        from axiom.extensions.builtins.lane.skills import lanes_path

        from . import lane_signal

        return lane_signal.annotate(payload, Registry(lanes_path()).all())
    except Exception:
        return payload


def emit(subject: str, payload: dict[str, Any] | None = None, *, bus: Any = None) -> bool:
    """Publish a RIVET lifecycle event. Returns True if published.

    ``bus`` is injectable for tests; when None, the process-default
    EventBus is used. Never raises — signalling must not break the caller.

    A branch-bearing event (a PR merge, a tag release) is annotated with the
    lane that branch was on before it goes out, so a subscriber like TIDY
    receives the lane instead of guessing it.
    """
    try:
        if bus is None:
            from axiom.infra.bus import get_default_eventbus

            bus = get_default_eventbus()
        bus.publish(subject, _with_lane(subject, payload or {}), source="rivet")
        return True
    except Exception:
        return False
