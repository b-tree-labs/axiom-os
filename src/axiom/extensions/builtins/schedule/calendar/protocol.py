# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The calendar provider contract — one Protocol every vendor implements.

Per prd-calendar-protocol.md: vendors declare an honest capability matrix, so
an unsupported operation fails at registration (``CapabilityUnsupported``), not
mid-sync. Data shapes are vendor-neutral; the adapter translates to/from the
vendor's native event resource.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Protocol, runtime_checkable


class CalendarCapability(str, Enum):
    LIST_EVENTS = "list_events"
    CREATE_EVENT = "create_event"
    UPDATE_EVENT = "update_event"
    DELETE_EVENT = "delete_event"
    CREATE_CALENDAR = "create_calendar"   # provision a new calendar
    MANAGE_ACL = "manage_acl"             # share a calendar with a user
    WATCH = "watch"            # push/poll change notifications
    INGEST_RSVPS = "ingest_rsvps"


class CapabilityUnsupported(Exception):
    """A provider was asked for a capability it does not declare."""


@dataclass(frozen=True)
class EventRef:
    """A stable handle to a vendor event."""

    vendor: str
    calendar_id: str
    event_id: str
    ical_uid: Optional[str] = None
    etag: Optional[str] = None


@dataclass
class EventSpec:
    """A vendor-neutral event. ``rrule`` carries iCalendar recurrence (RFC 5545);
    ``metadata`` is opaque round-tripped state (e.g. the PULSE slot/cadence id
    this event mirrors), so sync can re-find its own writes."""

    summary: str
    start: datetime
    end: Optional[datetime] = None
    rrule: Optional[str] = None
    timezone: str = "UTC"
    attendees: list[str] = field(default_factory=list)
    rsvps: dict = field(default_factory=dict)              # email -> accepted|declined|tentative|needsAction
    metadata: dict = field(default_factory=dict)
    ref: Optional[EventRef] = None
    # Enrichment carriers (vendor-neutral; the consumer fills the content) — turn
    # the event into a self-describing workflow hub instead of a dumb time-block.
    description: str = ""
    links: list[dict] = field(default_factory=list)        # [{"label", "url"}]
    reminders_minutes: list[int] = field(default_factory=list)
    color: Optional[str] = None                            # vendor color id
    status_line: Optional[str] = None                      # lifecycle state, shown up top
    organizer: Optional[str] = None                        # author principal/email (may be an AGENT, e.g. @AXI)
    thread_url: Optional[str] = None                       # linked realtime Slack/Teams thread (async<->sync bridge)


def render_description(spec: "EventSpec") -> str:
    """Assemble a human-readable description: a status line, the body, deep links,
    and a fenced machine block of the opaque metadata so the event is
    self-describing and survives export (e.g. a pushed-out .ics)."""
    import json

    parts: list[str] = []
    if spec.status_line:
        parts.append(spec.status_line)
    if spec.organizer:
        parts.append(f"Organizer: {spec.organizer}")        # may name an agent (@AXI)
    if spec.description:
        parts.append(spec.description)
    if spec.thread_url:
        parts.append(f"Live thread: {spec.thread_url}")       # realtime Slack/Teams bridge
    if spec.links:
        parts.append("\n".join(f"- {link['label']}: {link['url']}" for link in spec.links))
    if spec.metadata:
        parts.append("```axiom\n" + json.dumps(spec.metadata, sort_keys=True) + "\n```")
    return "\n\n".join(parts)


@runtime_checkable
class CalendarProvider(Protocol):
    """What a calendar vendor adapter must provide. ``capabilities`` is the
    honest support matrix; the factory checks it before dispatch."""

    vendor: str
    capabilities: frozenset[CalendarCapability]

    def health(self) -> bool: ...

    def list_events(
        self, *, calendar_id: str, start: datetime, end: datetime
    ) -> list[EventSpec]: ...

    def create_event(self, spec: EventSpec, *, calendar_id: str) -> EventRef: ...

    def update_event(self, ref: EventRef, patch: EventSpec) -> EventRef: ...

    def delete_event(self, ref: EventRef) -> None: ...

    def find_event(
        self, *, calendar_id: str, private_key: str, private_value: str
    ) -> Optional["EventSpec"]:
        """Find an event by a private extended-property stamp — the reconcile
        primitive (match PULSE's own writes regardless of time changes)."""
        ...

    # Capability-gated (CREATE_CALENDAR / MANAGE_ACL) — used by dead-simple setup.
    def create_calendar(self, *, summary: str, timezone: str = "UTC") -> str: ...

    def share_calendar(self, *, calendar_id: str, email: str, role: str = "writer") -> None: ...


def require(provider: CalendarProvider, capability: CalendarCapability) -> None:
    """Raise ``CapabilityUnsupported`` if the provider can't do ``capability``."""
    if capability not in provider.capabilities:
        raise CapabilityUnsupported(
            f"{provider.vendor} does not support {capability.value}"
        )


__all__ = [
    "CalendarCapability",
    "CalendarProvider",
    "CapabilityUnsupported",
    "EventRef",
    "EventSpec",
    "render_description",
    "require",
]
