# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Google Calendar provider — the first live adapter.

Conforms to the CalendarProvider Protocol; the only Google-specific parts are
auth (a service account with domain-wide delegation) and the event-resource
mapping. The Google API ``service`` (a googleapiclient discovery resource) is
**injectable** via ``config["service"]`` so the mapping + CRUD flow are unit-
tested with a fake — no network, no creds.

Recurrence: Google returns/accepts ``recurrence: ["RRULE:FREQ=...", ...]``; we
carry the single RRULE line on ``EventSpec.rrule`` and bind it to an ``rrule``
cadence (RFC 5545) via the codec. ``extendedProperties.private`` round-trips our
own metadata (the PULSE slot/cadence id), so sync can re-find its writes.

Production auth (service account):
  config = {
    "credentials_file": "/path/sa.json",   # service-account key JSON
    "subject": "user@domain",              # domain-wide-delegation impersonation
    "calendar_id": "<calendar id>",        # the dedicated calendar
  }
The service-account must have the Calendar API enabled, domain-wide delegation
granted for the Calendar scope, and the target calendar shared to it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from axiom.extensions.builtins.schedule.calendar.factory import register_vendor
from axiom.extensions.builtins.schedule.calendar.protocol import (
    CalendarCapability,
    EventRef,
    EventSpec,
    render_description,
)

_SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _parse_dt(node: dict) -> datetime:
    raw = node.get("dateTime") or node.get("date")
    return datetime.fromisoformat(raw)


def _extract_rrule(recurrence: Optional[list]) -> Optional[str]:
    for line in recurrence or []:
        if line.upper().startswith("RRULE:"):
            return line
    return None


def event_to_spec(event: dict, calendar_id: str) -> EventSpec:
    """Map a Google event resource → vendor-neutral EventSpec."""
    start_node = event.get("start", {})
    end_node = event.get("end", {})
    return EventSpec(
        summary=event.get("summary", ""),
        start=_parse_dt(start_node),
        end=_parse_dt(end_node) if end_node else None,
        rrule=_extract_rrule(event.get("recurrence")),
        timezone=start_node.get("timeZone", "UTC"),
        attendees=[a["email"] for a in event.get("attendees", []) if a.get("email")],
        rsvps={a["email"]: a.get("responseStatus", "needsAction")
               for a in event.get("attendees", []) if a.get("email")},
        metadata=dict(event.get("extendedProperties", {}).get("private", {})),
        ref=EventRef(
            vendor="google",
            calendar_id=calendar_id,
            event_id=event["id"],
            ical_uid=event.get("iCalUID"),
            etag=event.get("etag"),
        ),
    )


def spec_to_body(spec: EventSpec) -> dict:
    """Map a vendor-neutral EventSpec → a Google event resource body, including
    the enrichment carriers (rich description + links, reminders, color)."""
    body: dict[str, Any] = {
        "summary": spec.summary,
        "start": {"dateTime": spec.start.isoformat(), "timeZone": spec.timezone},
    }
    if spec.end is not None:
        body["end"] = {"dateTime": spec.end.isoformat(), "timeZone": spec.timezone}
    if spec.rrule:
        line = spec.rrule if spec.rrule.upper().startswith("RRULE:") else f"RRULE:{spec.rrule}"
        body["recurrence"] = [line]
    if spec.attendees:
        body["attendees"] = [{"email": e} for e in spec.attendees]
    if spec.metadata:
        body["extendedProperties"] = {"private": {k: str(v) for k, v in spec.metadata.items()}}
    description = render_description(spec)
    if description:
        body["description"] = description
    if spec.color:
        body["colorId"] = spec.color
    if spec.reminders_minutes:
        body["reminders"] = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": m} for m in spec.reminders_minutes],
        }
    return body


class GoogleCalendarProvider:
    vendor = "google"
    capabilities = frozenset({
        CalendarCapability.LIST_EVENTS,
        CalendarCapability.CREATE_EVENT,
        CalendarCapability.UPDATE_EVENT,
        CalendarCapability.DELETE_EVENT,
        CalendarCapability.CREATE_CALENDAR,
        CalendarCapability.MANAGE_ACL,
    })  # WATCH (push channels) + INGEST_RSVPS land with the sync loop.

    def __init__(self, config: Optional[dict] = None) -> None:
        config = config or {}
        self._service = config.get("service")  # injectable for tests
        self._config = config
        self.default_calendar_id = config.get("calendar_id", "primary")

    def _svc(self) -> Any:
        if self._service is None:
            self._service = _build_service(self._config)
        return self._service

    def health(self) -> bool:
        try:
            self._svc().calendarList().list().execute()
            return True
        except Exception:  # noqa: BLE001 — health never raises
            return False

    def list_events(
        self, *, calendar_id: str = "", start: datetime, end: datetime
    ) -> list[EventSpec]:
        cid = calendar_id or self.default_calendar_id
        resp = self._svc().events().list(
            calendarId=cid,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=False,   # keep recurrence intact (we want the RRULE)
        ).execute()
        return [event_to_spec(e, cid) for e in resp.get("items", [])]

    def create_event(self, spec: EventSpec, *, calendar_id: str = "") -> EventRef:
        cid = calendar_id or self.default_calendar_id
        created = self._svc().events().insert(
            calendarId=cid, body=spec_to_body(spec)
        ).execute()
        ref = event_to_spec(created, cid).ref
        spec.ref = ref
        return ref

    def update_event(self, ref: EventRef, patch: EventSpec) -> EventRef:
        updated = self._svc().events().update(
            calendarId=ref.calendar_id, eventId=ref.event_id, body=spec_to_body(patch)
        ).execute()
        return event_to_spec(updated, ref.calendar_id).ref

    def delete_event(self, ref: EventRef) -> None:
        self._svc().events().delete(
            calendarId=ref.calendar_id, eventId=ref.event_id
        ).execute()

    def find_event(
        self, *, calendar_id: str = "", private_key: str, private_value: str
    ) -> Optional[EventSpec]:
        cid = calendar_id or self.default_calendar_id
        resp = self._svc().events().list(
            calendarId=cid,
            privateExtendedProperty=f"{private_key}={private_value}",
            singleEvents=False,
            maxResults=1,
        ).execute()
        items = resp.get("items", [])
        return event_to_spec(items[0], cid) if items else None

    def create_calendar(self, *, summary: str, timezone: str = "UTC") -> str:
        """Create a calendar owned by the service account; returns its id."""
        created = self._svc().calendars().insert(
            body={"summary": summary, "timeZone": timezone}
        ).execute()
        return created["id"]

    def share_calendar(self, *, calendar_id: str, email: str, role: str = "writer") -> None:
        """Grant a user access to a calendar (so an SA-owned calendar shows up
        in their UI). ``role`` is reader | writer | owner."""
        self._svc().acl().insert(
            calendarId=calendar_id,
            body={"role": role, "scope": {"type": "user", "value": email}},
        ).execute()


def _build_service(config: dict) -> Any:
    """Build a Google Calendar API service from service-account creds.

    Credentials come from (in priority): ``credentials_ref`` (a SecretRef into
    the secrets vault — preferred, no loose key on disk), ``credentials_info``
    (a dict), or ``credentials_file`` (a path).
    """
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "google-api-python-client + google-auth are required for the Google "
            "calendar provider; install the optional [calendar] extra."
        ) from exc

    # Delegated user OAuth / SSO sign-in (access_token, a refreshing token_source,
    # or a pre-built Credentials) takes priority over the app-only service account.
    if config.get("oauth_credentials") is not None:
        creds = config["oauth_credentials"]
    elif config.get("access_token") or config.get("token_source"):
        from google.oauth2.credentials import Credentials

        token = config["token_source"]() if config.get("token_source") else config["access_token"]
        creds = Credentials(token=token, scopes=_SCOPES)
    elif config.get("credentials_ref"):
        import json

        from axiom.extensions.builtins.secrets import SecretRef, resolve

        with resolve(SecretRef.parse(config["credentials_ref"])) as secret:
            info = json.loads(secret.value)
        creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
    elif config.get("credentials_info"):
        creds = service_account.Credentials.from_service_account_info(
            config["credentials_info"], scopes=_SCOPES
        )
    elif config.get("credentials_file"):
        creds = service_account.Credentials.from_service_account_file(
            config["credentials_file"], scopes=_SCOPES
        )
    else:
        raise RuntimeError(
            "Google calendar provider needs credentials_ref / credentials_info / "
            "credentials_file in its config."
        )

    subject = config.get("subject")
    if subject:
        creds = creds.with_subject(subject)  # domain-wide delegation
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


register_vendor("google", lambda config: GoogleCalendarProvider(config))


__all__ = ["GoogleCalendarProvider", "event_to_spec", "spec_to_body"]
