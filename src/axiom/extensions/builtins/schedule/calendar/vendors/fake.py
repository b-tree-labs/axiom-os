# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""In-memory calendar provider — the deterministic test vehicle.

Implements the full CalendarProvider Protocol with a dict backing store, so all
sync + binding logic is testable without a network or vendor creds. The live
Google / M365 adapters are thin conformance layers over the same Protocol.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from axiom.extensions.builtins.schedule.calendar.factory import register_vendor
from axiom.extensions.builtins.schedule.calendar.protocol import (
    CalendarCapability,
    EventRef,
    EventSpec,
)


class FakeCalendarProvider:
    vendor = "fake"
    capabilities = frozenset(CalendarCapability)  # supports everything

    def __init__(self, config: Optional[dict] = None) -> None:
        self._events: dict[str, EventSpec] = {}
        self._healthy = (config or {}).get("healthy", True)
        self.calendars: dict[str, dict] = {}     # provisioned calendars
        self.acl: dict[str, list] = {}           # calendar_id -> [(email, role)]

    def health(self) -> bool:
        return self._healthy

    def list_events(
        self, *, calendar_id: str = "primary", start: datetime, end: datetime
    ) -> list[EventSpec]:
        return [
            s for s in self._events.values()
            if start <= s.start < end
        ]

    def create_event(self, spec: EventSpec, *, calendar_id: str = "primary") -> EventRef:
        eid = uuid.uuid4().hex
        ref = EventRef(vendor="fake", calendar_id=calendar_id, event_id=eid, ical_uid=eid)
        spec.ref = ref
        self._events[eid] = spec
        return ref

    def update_event(self, ref: EventRef, patch: EventSpec) -> EventRef:
        if ref.event_id not in self._events:
            raise KeyError(f"no such event: {ref.event_id}")
        patch.ref = ref
        self._events[ref.event_id] = patch
        return ref

    def delete_event(self, ref: EventRef) -> None:
        self._events.pop(ref.event_id, None)

    def find_event(
        self, *, calendar_id: str = "primary", private_key: str, private_value: str
    ) -> Optional[EventSpec]:
        for spec in self._events.values():
            if spec.metadata.get(private_key) == private_value:
                return spec
        return None

    def create_calendar(self, *, summary: str, timezone: str = "UTC") -> str:
        cid = "fakecal-" + uuid.uuid4().hex[:8]
        self.calendars[cid] = {"summary": summary, "timeZone": timezone}
        return cid

    def share_calendar(self, *, calendar_id: str, email: str, role: str = "writer") -> None:
        self.acl.setdefault(calendar_id, []).append((email, role))


register_vendor("fake", lambda config: FakeCalendarProvider(config))


__all__ = ["FakeCalendarProvider"]
