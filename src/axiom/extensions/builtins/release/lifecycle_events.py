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


def emit(subject: str, payload: dict[str, Any] | None = None, *, bus: Any = None) -> bool:
    """Publish a RIVET lifecycle event. Returns True if published.

    ``bus`` is injectable for tests; when None, the process-default
    EventBus is used. Never raises — signalling must not break the caller.
    """
    try:
        if bus is None:
            from axiom.infra.bus import get_default_eventbus

            bus = get_default_eventbus()
        bus.publish(subject, payload or {}, source="rivet")
        return True
    except Exception:
        return False
