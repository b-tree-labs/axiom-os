# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""CalDAV calendar provider (track F) — Apple iCloud + Fastmail / Nextcloud /
Zimbra / Yahoo / self-hosted (Radicale, Baïkal).

Same CalendarProvider Protocol. CalDAV speaks **iCalendar** (RFC 5545) over HTTP,
so RRULE maps **directly** (no conversion, unlike Graph). The stamp rides an
``X-AXIOM-*`` property. The CalDAV HTTP client (PUT/DELETE/REPORT) is injectable,
so the iCal mapping + CRUD are unit-tested against a fake — no network.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Optional

from axiom.extensions.builtins.schedule.calendar.factory import register_vendor
from axiom.extensions.builtins.schedule.calendar.protocol import (
    CalendarCapability,
    EventRef,
    EventSpec,
    render_description,
)


def _ical_dt(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _parse_dt(value: str) -> datetime:
    return datetime.strptime(value.strip(), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def spec_to_ical(spec: EventSpec, *, uid: Optional[str] = None) -> str:
    """Serialize an EventSpec to an iCalendar VEVENT."""
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Axiom//PULSE//EN",
        "BEGIN:VEVENT",
        f"UID:{uid or uuid.uuid4().hex}",
        f"SUMMARY:{spec.summary}",
        f"DTSTART:{_ical_dt(spec.start)}",
    ]
    if spec.end is not None:
        lines.append(f"DTEND:{_ical_dt(spec.end)}")
    if spec.rrule:
        lines.append(spec.rrule if spec.rrule.upper().startswith("RRULE:") else f"RRULE:{spec.rrule}")
    for attendee in spec.attendees:
        lines.append(f"ATTENDEE:mailto:{attendee}")
    for key, value in (spec.metadata or {}).items():
        lines.append(f"X-AXIOM-{key.upper()}:{value}")
    description = render_description(spec)
    if description:
        lines.append("DESCRIPTION:" + description.replace("\r\n", "\\n").replace("\n", "\\n"))
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines)


def ical_to_spec(ical: str, calendar_id: str, *, href: Optional[str] = None) -> EventSpec:
    """Parse an iCalendar VEVENT back to an EventSpec."""
    summary, start, end, rrule, uid = "", None, None, None, None
    attendees: list = []
    metadata: dict = {}
    for raw in ical.replace("\r\n", "\n").split("\n"):
        if ":" not in raw:
            continue
        key, _, val = raw.partition(":")
        key = key.split(";")[0].upper()
        if key == "SUMMARY":
            summary = val
        elif key == "UID":
            uid = val
        elif key == "DTSTART":
            start = _parse_dt(val)
        elif key == "DTEND":
            end = _parse_dt(val)
        elif key == "RRULE":
            rrule = f"RRULE:{val}"
        elif key == "ATTENDEE":
            attendees.append(val.replace("mailto:", ""))
        elif key.startswith("X-AXIOM-"):
            metadata[key[len("X-AXIOM-"):].lower()] = val
    return EventSpec(
        summary=summary, start=start, end=end, rrule=rrule, attendees=attendees,
        metadata=metadata,
        ref=EventRef(vendor="caldav", calendar_id=calendar_id,
                     event_id=href or f"{uid}.ics", ical_uid=uid),
    )


class CalDAVCalendarProvider:
    vendor = "caldav"
    capabilities = frozenset({
        CalendarCapability.LIST_EVENTS,
        CalendarCapability.CREATE_EVENT,
        CalendarCapability.UPDATE_EVENT,
        CalendarCapability.DELETE_EVENT,
    })

    def __init__(self, config: Optional[dict] = None) -> None:
        config = config or {}
        self._client = config.get("client")   # injectable; PUT/DELETE/REPORT over HTTP
        self.default_calendar_id = config.get("calendar_id", "")

    def _c(self):
        if self._client is None:
            raise RuntimeError("CalDAV client not configured (url/username/password).")
        return self._client

    def health(self) -> bool:
        try:
            self._c().query(self.default_calendar_id, None, None)
            return True
        except Exception:  # noqa: BLE001
            return False

    def list_events(self, *, calendar_id: str = "", start=None, end=None) -> list:
        cid = calendar_id or self.default_calendar_id
        return [ical_to_spec(item["ical"], cid, href=item.get("href"))
                for item in self._c().query(cid, start, end)]

    def create_event(self, spec: EventSpec, *, calendar_id: str = "") -> EventRef:
        cid = calendar_id or self.default_calendar_id
        uid = uuid.uuid4().hex
        href = f"{cid}/{uid}.ics"
        self._c().put(href, spec_to_ical(spec, uid=uid))
        ref = EventRef(vendor="caldav", calendar_id=cid, event_id=href, ical_uid=uid)
        spec.ref = ref
        return ref

    def update_event(self, ref: EventRef, patch: EventSpec) -> EventRef:
        self._c().put(ref.event_id, spec_to_ical(patch, uid=ref.ical_uid))
        return ref

    def delete_event(self, ref: EventRef) -> None:
        self._c().delete(ref.event_id)

    def find_event(self, *, calendar_id: str = "", private_key: str, private_value: str) -> Optional[EventSpec]:
        for spec in self.list_events(calendar_id=calendar_id):
            if spec.metadata.get(private_key) == private_value:
                return spec
        return None


register_vendor("caldav", lambda config: CalDAVCalendarProvider(config))


__all__ = ["CalDAVCalendarProvider", "ical_to_spec", "spec_to_ical"]
