# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Microsoft 365 (Graph) calendar provider.

Conforms to the CalendarProvider Protocol. Two Graph-specific concerns:

1. **Recurrence is a structured object** (``pattern`` + ``range``), not an iCal
   RRULE string — so we convert RRULE ⇄ Graph PatternedRecurrence (daily /
   weekly / monthly + interval + count/until).
2. **The reconcile stamp** rides a ``singleValueExtendedProperties`` named
   property (Graph's equivalent of Google's private extended property), which
   ``$filter`` can match.

The Graph HTTP client is **injectable** (``config["client"]``) — an object with
``request(method, path, *, json=None, params=None) -> dict`` — so the mapping +
CRUD are unit-tested against a fake, no creds/network. Production auth is MSAL
client-credentials (Entra app registration; see vendors/m365-setup.md).
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
from axiom.extensions.builtins.schedule.formats import FormatError

# A stable named MAPI property for the reconcile stamp.
STAMP_GUID = "66f5a359-4659-4830-9070-00047ec6ac6e"
STAMP_NAME = "pulse_slot_id"
_STAMP_ID = f"String {{{STAMP_GUID}}} Name {STAMP_NAME}"

_DOW = {"MO": "monday", "TU": "tuesday", "WE": "wednesday", "TH": "thursday",
        "FR": "friday", "SA": "saturday", "SU": "sunday"}
_DOW_R = {v: k for k, v in _DOW.items()}
_WEEKDAY = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


# --- recurrence conversion (the distinctive Graph work) -----------------------

def rrule_to_graph(rrule: str, start: datetime) -> dict:
    """iCal RRULE → Graph PatternedRecurrence (common patterns)."""
    body = rrule.split(":", 1)[-1] if rrule.upper().startswith("RRULE:") else rrule
    parts = dict(kv.split("=", 1) for kv in body.split(";") if "=" in kv)
    freq = parts.get("FREQ", "").upper()
    interval = int(parts.get("INTERVAL", "1"))
    pattern: dict[str, Any] = {"interval": interval}
    if freq == "DAILY":
        pattern["type"] = "daily"
    elif freq == "WEEKLY":
        pattern["type"] = "weekly"
        days = [d for d in parts.get("BYDAY", "").split(",") if d]
        pattern["daysOfWeek"] = [_DOW[d] for d in days] or [_DOW[_WEEKDAY[start.weekday()]]]
    elif freq == "MONTHLY":
        pattern["type"] = "absoluteMonthly"
        pattern["dayOfMonth"] = start.day
    else:
        raise FormatError(f"RRULE FREQ={freq!r} not yet mapped to Graph recurrence")
    rng: dict[str, Any] = {"type": "noEnd", "startDate": start.date().isoformat()}
    if "COUNT" in parts:
        rng = {"type": "numbered", "startDate": start.date().isoformat(),
               "numberOfOccurrences": int(parts["COUNT"])}
    elif "UNTIL" in parts:
        rng = {"type": "endDate", "startDate": start.date().isoformat(),
               "endDate": parts["UNTIL"][:8]}  # YYYYMMDD prefix
    return {"pattern": pattern, "range": rng}


def graph_to_rrule(recurrence: dict) -> str:
    """Graph PatternedRecurrence → iCal RRULE."""
    pattern = recurrence.get("pattern", {})
    rng = recurrence.get("range", {})
    ptype = pattern.get("type")
    interval = pattern.get("interval", 1)
    if ptype == "daily":
        rule = "FREQ=DAILY"
    elif ptype == "weekly":
        days = ",".join(_DOW_R[d] for d in pattern.get("daysOfWeek", []))
        rule = "FREQ=WEEKLY" + (f";BYDAY={days}" if days else "")
    elif ptype in ("absoluteMonthly", "relativeMonthly"):
        rule = "FREQ=MONTHLY"
    else:
        raise FormatError(f"Graph recurrence type {ptype!r} not mapped to RRULE")
    if interval and interval > 1:
        rule += f";INTERVAL={interval}"
    if rng.get("type") == "numbered":
        rule += f";COUNT={rng['numberOfOccurrences']}"
    elif rng.get("type") == "endDate":
        rule += f";UNTIL={rng['endDate'].replace('-', '')}"
    return f"RRULE:{rule}"


# --- event mapping ------------------------------------------------------------

def event_to_spec(event: dict, calendar_id: str) -> EventSpec:
    start_node = event.get("start", {})
    end_node = event.get("end", {})
    stamp = None
    for ep in event.get("singleValueExtendedProperties", []):
        if ep.get("id") == _STAMP_ID:
            stamp = ep.get("value")
    return EventSpec(
        summary=event.get("subject", ""),
        start=datetime.fromisoformat(start_node["dateTime"]) if start_node else None,
        end=datetime.fromisoformat(end_node["dateTime"]) if end_node.get("dateTime") else None,
        rrule=graph_to_rrule(event["recurrence"]) if event.get("recurrence") else None,
        timezone=start_node.get("timeZone", "UTC"),
        attendees=[a["emailAddress"]["address"] for a in event.get("attendees", [])
                   if a.get("emailAddress", {}).get("address")],
        rsvps={a["emailAddress"]["address"]: a.get("status", {}).get("response", "none")
               for a in event.get("attendees", []) if a.get("emailAddress", {}).get("address")},
        metadata={STAMP_NAME: stamp} if stamp else {},
        ref=EventRef(vendor="m365", calendar_id=calendar_id, event_id=event["id"],
                     ical_uid=event.get("iCalUId"), etag=event.get("@odata.etag")),
    )


def spec_to_graph(spec: EventSpec) -> dict:
    body: dict[str, Any] = {
        "subject": spec.summary,
        "start": {"dateTime": spec.start.isoformat(), "timeZone": spec.timezone},
        "body": {"contentType": "text", "content": render_description(spec)},
    }
    if spec.end is not None:
        body["end"] = {"dateTime": spec.end.isoformat(), "timeZone": spec.timezone}
    if spec.rrule:
        body["recurrence"] = rrule_to_graph(spec.rrule, spec.start)
    if spec.attendees:
        body["attendees"] = [{"emailAddress": {"address": e}, "type": "required"}
                             for e in spec.attendees]
    stamp = spec.metadata.get(STAMP_NAME)
    if stamp is not None:
        body["singleValueExtendedProperties"] = [{"id": _STAMP_ID, "value": str(stamp)}]
    return body


class M365CalendarProvider:
    vendor = "m365"
    capabilities = frozenset({
        CalendarCapability.LIST_EVENTS,
        CalendarCapability.CREATE_EVENT,
        CalendarCapability.UPDATE_EVENT,
        CalendarCapability.DELETE_EVENT,
        CalendarCapability.CREATE_CALENDAR,  # in the target mailbox (no share needed)
    })  # MANAGE_ACL + WATCH land next.

    def __init__(self, config: Optional[dict] = None) -> None:
        config = config or {}
        self._client = config.get("client")  # injectable for tests
        self._config = config
        self.user_id = config.get("user_id")
        self.default_calendar_id = config.get("calendar_id", "")

    def _c(self) -> Any:
        if self._client is None:
            self._client = _build_client(self._config)
        return self._client

    def _base(self, calendar_id: str) -> str:
        cid = calendar_id or self.default_calendar_id
        root = f"/users/{self.user_id}"
        return f"{root}/calendars/{cid}" if cid else f"{root}/calendar"

    def health(self) -> bool:
        try:
            self._c().request("GET", f"/users/{self.user_id}/calendar")
            return True
        except Exception:  # noqa: BLE001
            return False

    def list_events(self, *, calendar_id: str = "", start: datetime, end: datetime) -> list:
        cid = calendar_id or self.default_calendar_id
        resp = self._c().request(
            "GET", f"{self._base(cid)}/events",
            params={"$top": 250,
                    "startDateTime": start.isoformat(), "endDateTime": end.isoformat()},
        )
        return [event_to_spec(e, cid) for e in resp.get("value", [])]

    def create_event(self, spec: EventSpec, *, calendar_id: str = "") -> EventRef:
        cid = calendar_id or self.default_calendar_id
        created = self._c().request("POST", f"{self._base(cid)}/events", json=spec_to_graph(spec))
        ref = event_to_spec(created, cid).ref
        spec.ref = ref
        return ref

    def update_event(self, ref: EventRef, patch: EventSpec) -> EventRef:
        updated = self._c().request(
            "PATCH", f"/users/{self.user_id}/events/{ref.event_id}", json=spec_to_graph(patch))
        return event_to_spec(updated, ref.calendar_id).ref

    def delete_event(self, ref: EventRef) -> None:
        self._c().request("DELETE", f"/users/{self.user_id}/events/{ref.event_id}")

    def create_calendar(self, *, summary: str, timezone: str = "UTC") -> str:
        """Create a calendar in the target mailbox; returns its id. No sharing
        needed — it already lives in the user's own mailbox."""
        created = self._c().request(
            "POST", f"/users/{self.user_id}/calendars", json={"name": summary})
        return created["id"]

    def find_event(self, *, calendar_id: str = "", private_key: str, private_value: str) -> Optional[EventSpec]:
        cid = calendar_id or self.default_calendar_id
        flt = (f"singleValueExtendedProperties/any(ep: ep/id eq '{_STAMP_ID}' "
               f"and ep/value eq '{private_value}')")
        resp = self._c().request(
            "GET", f"{self._base(cid)}/events",
            params={"$filter": flt,
                    "$expand": f"singleValueExtendedProperties($filter=id eq '{_STAMP_ID}')"},
        )
        items = resp.get("value", [])
        return event_to_spec(items[0], cid) if items else None


class _GraphClient:
    """A thin Graph HTTP client over a bearer token. The token can come from
    app-only MSAL *or* a delegated user OAuth/SSO session — the calendar logic
    doesn't care which."""

    base = "https://graph.microsoft.com/v1.0"

    def __init__(self, access_token: str, token_source: Optional[Any] = None) -> None:
        self._token = access_token
        self._token_source = token_source  # callable -> fresh token (OAuth refresh)

    def _headers(self) -> dict:
        token = self._token_source() if self._token_source is not None else self._token
        return {"Authorization": f"Bearer {token}"}

    def request(self, method, path, *, json=None, params=None):
        import requests

        resp = requests.request(method, self.base + path, headers=self._headers(),
                                json=json, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json() if resp.content else {}


def _build_client(config: dict) -> Any:
    """Build a Graph client. Auth modes (priority):

    - ``token_source`` — a callable returning a fresh bearer token (the seam a
      delegated OAuth/SSO session plugs into; refresh handled upstream).
    - ``access_token`` — a delegated user OAuth token (SSO sign-in).
    - app-only **MSAL client-credentials** (``tenant_id``/``client_id``/
      ``client_secret``) for an unattended central scheduler.
    """
    if config.get("token_source") is not None:
        return _GraphClient("", token_source=config["token_source"])
    if config.get("access_token"):
        return _GraphClient(config["access_token"])
    try:
        import msal
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("msal is required for app-only auth; install the [m365] extra.") from exc
    app = msal.ConfidentialClientApplication(
        client_id=config["client_id"],
        authority=f"https://login.microsoftonline.com/{config['tenant_id']}",
        client_credential=config["client_secret"],
    )
    token = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in token:
        raise RuntimeError(f"Graph auth failed: {token.get('error_description', token)}")
    return _GraphClient(token["access_token"])


register_vendor("m365", lambda config: M365CalendarProvider(config))


__all__ = ["M365CalendarProvider", "event_to_spec", "graph_to_rrule",
           "rrule_to_graph", "spec_to_graph"]
