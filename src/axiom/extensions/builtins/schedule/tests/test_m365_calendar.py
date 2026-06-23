# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""M365 (Graph) calendar provider — the RRULE⇄Graph-recurrence converter and the
event mapping + CRUD, tested against a fake Graph client (no creds/network)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.schedule import calendar
from axiom.extensions.builtins.schedule.calendar.protocol import EventSpec
from axiom.extensions.builtins.schedule.calendar.vendors import m365

MON = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)  # a Monday
HOUR = timedelta(hours=1)


# --- a fake Graph HTTP client -------------------------------------------------

def _stamp_of(event):
    for ep in event.get("singleValueExtendedProperties", []):
        if ep.get("id") == m365._STAMP_ID:
            return ep.get("value")
    return None


class _FakeGraph:
    def __init__(self):
        self.events = {}

    def request(self, method, path, *, json=None, params=None):
        params = params or {}
        if method == "GET" and path.endswith("/calendar"):
            return {}  # health
        if method == "POST" and path.endswith("/events"):
            eid = f"evt-{len(self.events) + 1}"
            e = dict(json, id=eid)
            e["@odata.etag"] = 'W/"1"'
            self.events[eid] = e
            return e
        if method == "GET" and path.endswith("/events"):
            items = list(self.events.values())
            flt = params.get("$filter")
            if flt:
                m = re.search(r"ep/value eq '([^']*)'", flt)
                want = m.group(1) if m else None
                items = [e for e in items if _stamp_of(e) == want]
            return {"value": items}
        if method == "PATCH":
            eid = path.rsplit("/", 1)[-1]
            e = dict(self.events[eid], **json, id=eid)
            self.events[eid] = e
            return e
        if method == "DELETE":
            self.events.pop(path.rsplit("/", 1)[-1], None)
            return {}
        return {}


def _provider():
    return calendar.get_provider("m365", {"client": _FakeGraph(), "user_id": "u@x.com"})


# --- recurrence conversion ----------------------------------------------------

def test_rrule_to_graph_weekly():
    rec = m365.rrule_to_graph("RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR", MON)
    assert rec["pattern"]["type"] == "weekly"
    assert rec["pattern"]["daysOfWeek"] == ["monday", "wednesday", "friday"]
    assert rec["range"]["type"] == "noEnd"


def test_rrule_to_graph_count_and_interval():
    rec = m365.rrule_to_graph("RRULE:FREQ=DAILY;INTERVAL=2;COUNT=5", MON)
    assert rec["pattern"] == {"interval": 2, "type": "daily"}
    assert rec["range"]["type"] == "numbered" and rec["range"]["numberOfOccurrences"] == 5


def test_recurrence_round_trips():
    for rrule in ("RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR",
                  "RRULE:FREQ=DAILY;INTERVAL=2;COUNT=5",
                  "RRULE:FREQ=MONTHLY"):
        assert m365.graph_to_rrule(m365.rrule_to_graph(rrule, MON)) == rrule


# --- event mapping ------------------------------------------------------------

def test_spec_to_graph_and_back():
    spec = EventSpec(summary="Sync", start=MON, end=MON + HOUR,
                     rrule="RRULE:FREQ=WEEKLY;BYDAY=WE", attendees=["a@x.com"],
                     metadata={m365.STAMP_NAME: "slot-1"})
    body = m365.spec_to_graph(spec)
    assert body["subject"] == "Sync"
    assert body["recurrence"]["pattern"]["type"] == "weekly"
    assert body["attendees"][0]["emailAddress"]["address"] == "a@x.com"
    assert body["singleValueExtendedProperties"][0]["value"] == "slot-1"

    body["id"] = "e1"
    back = m365.event_to_spec(body, "cal")
    assert back.summary == "Sync" and back.rrule == "RRULE:FREQ=WEEKLY;BYDAY=WE"
    assert back.metadata[m365.STAMP_NAME] == "slot-1"


# --- CRUD via the fake client -------------------------------------------------

def test_provider_crud_and_find_by_stamp():
    p = _provider()
    assert p.health() is True
    ref = p.create_event(EventSpec(summary="Reserved", start=MON,
                                   rrule="RRULE:FREQ=DAILY",
                                   metadata={m365.STAMP_NAME: "slot-9"}))
    assert ref.vendor == "m365"

    found = p.find_event(private_key=m365.STAMP_NAME, private_value="slot-9")
    assert found is not None and found.summary == "Reserved"
    assert found.rrule == "RRULE:FREQ=DAILY"

    p.update_event(ref, EventSpec(summary="Reserved v2", start=MON))
    assert p.list_events(start=MON - HOUR, end=MON + HOUR)[0].summary == "Reserved v2"

    p.delete_event(ref)
    assert p.find_event(private_key=m365.STAMP_NAME, private_value="slot-9") is None


def test_delegated_oauth_token_paths():
    # OAuth/SSO: a delegated bearer token, or a refreshing token_source, builds
    # a client without app-only MSAL — the seam an institution's SSO plugs into.
    c1 = m365._build_client({"access_token": "user-sso-token"})
    assert c1._headers()["Authorization"] == "Bearer user-sso-token"
    c2 = m365._build_client({"token_source": lambda: "fresh-after-refresh"})
    assert c2._headers()["Authorization"] == "Bearer fresh-after-refresh"
