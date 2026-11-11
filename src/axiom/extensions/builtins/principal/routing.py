# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Deterministic topic → channel routing.

Spec §4. Three properties are deliberate:

- **Unverified is unusable.** A channel that never round-tripped is not a channel,
  regardless of how correct its configuration looks.
- **Quiet hours defer, never drop.** A deferred message keeps its destination and
  gains a time; it does not evaporate.
- **Silence is never an outcome.** When nothing preferred is usable, the local
  inbox takes it and ``unreachable`` is raised so the caller knows the person was
  not really reached.

Every decision carries its own explanation, because routing you cannot interrogate
after an incident is routing you cannot trust during one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import ContactEndpoint, EndpointHealth, PrincipalProfile

__all__ = ["Decision", "route"]


@dataclass
class Decision:
    """Where a message goes, when, and why — including what was passed over."""

    endpoint: ContactEndpoint | None
    explain: str
    escalation: list[ContactEndpoint] = field(default_factory=list)
    rejected_reasons: dict[str, str] = field(default_factory=dict)
    deferred_until: datetime | None = None
    unreachable: bool = False


def _parse_hhmm(value: str) -> tuple[int, int]:
    hh, mm = value.split(":")
    return int(hh), int(mm)


def _in_quiet_hours(now: datetime, window: tuple[str, str]) -> bool:
    """Quiet-hours membership, correct across midnight.

    Start is inclusive and end exclusive, so a 22:00–07:00 window contains 22:00
    and 06:59 but not 07:00 — the boundaries a person actually means.
    """
    start_h, start_m = _parse_hhmm(window[0])
    end_h, end_m = _parse_hhmm(window[1])
    minute = now.hour * 60 + now.minute
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    if start == end:
        return False
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end  # wraps midnight


def _next_end(now: datetime, window: tuple[str, str]) -> datetime:
    end_h, end_m = _parse_hhmm(window[1])
    candidate = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def route(
    principal: PrincipalProfile,
    *,
    topic: str,
    urgency: int,
    now: datetime,
) -> Decision:
    """Choose an endpoint for ``topic`` at ``now``, explaining every rejection."""
    rejected: dict[str, str] = {}
    pref = principal.preference_for(topic)

    if pref is None:
        inbox = principal.endpoint("inbox")
        return Decision(
            endpoint=inbox,
            explain=f"no preference for topic {topic!r} and no wildcard; falling back to inbox",
            rejected_reasons=rejected,
            unreachable=inbox is None or not inbox.is_verified,
        )

    usable: list[ContactEndpoint] = []
    for kind in pref.ranked_kinds:
        ep = principal.endpoint(kind)
        if ep is None:
            rejected[kind] = "no endpoint of this kind on the principal record"
            continue
        if not ep.is_verified:
            rejected[kind] = "unverified — never completed a round trip, so it is not a channel"
            continue
        if ep.health is EndpointHealth.FAILED:
            rejected[kind] = f"failed: {ep.health_reason or 'unknown reason'}"
            continue
        usable.append(ep)

    if not usable:
        # Never drop. The local inbox is always present and always reachable, but
        # landing here means the person was not actually reached — say so loudly.
        inbox = principal.endpoint("inbox")
        return Decision(
            endpoint=inbox,
            explain=(
                f"no usable channel for topic {topic!r} "
                f"({', '.join(f'{k}: {v}' for k, v in rejected.items()) or 'none configured'}); "
                "falling back to inbox"
            ),
            rejected_reasons=rejected,
            unreachable=True,
        )

    chosen, escalation = usable[0], usable[1:]
    deferred_until = None
    explain = f"topic {topic!r} → {chosen.kind} (rank 1 of {len(usable)} usable)"

    if principal.quiet_hours and _in_quiet_hours(now, principal.quiet_hours):
        if urgency < pref.urgency_floor:
            deferred_until = _next_end(now, principal.quiet_hours)
            explain += (
                f"; deferred to {deferred_until.isoformat()} — urgency {urgency} "
                f"is below the floor of {pref.urgency_floor} during quiet hours"
            )
        else:
            explain += (
                f"; quiet hours in effect but urgency {urgency} meets the floor "
                f"of {pref.urgency_floor}, so it goes now"
            )

    if chosen.health is EndpointHealth.DEGRADED:
        explain += f"; note: endpoint is degraded ({chosen.health_reason or 'reason unrecorded'})"

    return Decision(
        endpoint=chosen,
        explain=explain,
        escalation=escalation,
        rejected_reasons=rejected,
        deferred_until=deferred_until,
    )
