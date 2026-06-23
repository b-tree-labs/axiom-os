# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Calendar Context — correlate signals with calendar events to enrich metadata.

When a signal arrives with a timestamp, we can look up what meeting was happening
(or just ended) and hydrate the signal with:
- Meeting title and description
- Attendees (potential FYI recipients)
- Organizer (likely stakeholder)
- Recurring series info (helps classify signal topic)
- Linked documents (agenda, meeting notes)

Provider-agnostic design:
- GoogleCalendarProvider (OAuth2)
- OutlookCalendarProvider (Graph API)
- ICalFileProvider (local .ics files for testing)

Usage:
    from axiom.extensions.builtins.signals.calendar_context import CalendarContext

    ctx = CalendarContext()
    event = ctx.find_event_at(signal.timestamp)
    if event:
        signal.metadata["meeting"] = event.to_dict()
        # Suggest attendees as FYI recipients
        suggested_people = event.attendees
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.state import atomic_write

_RUNTIME_DIR = _REPO_ROOT / "runtime"
CONFIG_DIR = _RUNTIME_DIR / "config"
CALENDAR_CACHE = _RUNTIME_DIR / "inbox" / "cache" / "calendar_events.json"


@dataclass
class CalendarEvent:
    """A calendar event that may correlate with a signal."""

    event_id: str
    title: str
    start: datetime
    end: datetime

    # People
    organizer: str = ""
    attendees: list[str] = field(default_factory=list)

    # Context
    description: str = ""
    location: str = ""

    # Classification hints
    is_recurring: bool = False
    recurrence_name: str = ""  # e.g., "Weekly Project Standup"

    # Links
    meeting_link: str = ""  # Zoom/Teams/Meet URL
    attached_docs: list[str] = field(default_factory=list)

    # Provider metadata
    provider: str = ""
    calendar_name: str = ""

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "title": self.title,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "organizer": self.organizer,
            "attendees": self.attendees,
            "description": self.description[:500] if self.description else "",
            "location": self.location,
            "is_recurring": self.is_recurring,
            "recurrence_name": self.recurrence_name,
            "meeting_link": self.meeting_link,
            "attached_docs": self.attached_docs,
            "provider": self.provider,
            "calendar_name": self.calendar_name,
        }

    @classmethod
    def from_dict(cls, data: dict) -> CalendarEvent:
        start = datetime.fromisoformat(data["start"])
        end = datetime.fromisoformat(data["end"])
        return cls(
            event_id=data.get("event_id", ""),
            title=data.get("title", ""),
            start=start,
            end=end,
            organizer=data.get("organizer", ""),
            attendees=data.get("attendees", []),
            description=data.get("description", ""),
            location=data.get("location", ""),
            is_recurring=data.get("is_recurring", False),
            recurrence_name=data.get("recurrence_name", ""),
            meeting_link=data.get("meeting_link", ""),
            attached_docs=data.get("attached_docs", []),
            provider=data.get("provider", ""),
            calendar_name=data.get("calendar_name", ""),
        )

    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() / 60)

    def attendee_emails(self) -> list[str]:
        """Extract email addresses from attendees list."""
        emails = []
        for att in self.attendees:
            if "@" in att:
                # Could be "Name <email>" or just "email"
                if "<" in att and ">" in att:
                    email = att.split("<")[1].split(">")[0]
                else:
                    email = att
                emails.append(email.strip().lower())
        return emails


class CalendarProvider(ABC):
    """Abstract base for calendar data providers."""

    @abstractmethod
    def get_events(
        self,
        start: datetime,
        end: datetime,
        calendar_id: str | None = None,
    ) -> list[CalendarEvent]:
        """Fetch events in the given time range."""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this provider is configured and usable."""
        pass


class ICalFileProvider(CalendarProvider):
    """Load events from local .ics files (for testing or exported calendars)."""

    def __init__(self, ics_path: Path | None = None):
        self.ics_path = ics_path or (CONFIG_DIR / "calendar.ics")

    def is_available(self) -> bool:
        return self.ics_path.exists()

    def get_events(
        self,
        start: datetime,
        end: datetime,
        calendar_id: str | None = None,
    ) -> list[CalendarEvent]:
        if not self.is_available():
            return []

        try:
            # Use icalendar library if available
            from icalendar import Calendar  # type: ignore[import-untyped]

            cal = Calendar.from_ical(self.ics_path.read_bytes())  # type: ignore[arg-type]
            events = []

            for component in cal.walk():
                if component.name != "VEVENT":
                    continue

                dtstart = component.get("dtstart")
                dtend = component.get("dtend")

                if not dtstart:
                    continue

                event_start = dtstart.dt
                if isinstance(event_start, datetime):
                    if event_start.tzinfo is None:
                        event_start = event_start.replace(tzinfo=UTC)
                else:
                    # Date only, convert to datetime
                    event_start = datetime.combine(
                        event_start, datetime.min.time(), tzinfo=UTC
                    )

                event_end = event_start + timedelta(hours=1)  # Default 1 hour
                if dtend:
                    end_dt = dtend.dt
                    if isinstance(end_dt, datetime):
                        if end_dt.tzinfo is None:
                            end_dt = end_dt.replace(tzinfo=UTC)
                        event_end = end_dt

                # Filter by time range
                if event_end < start or event_start > end:
                    continue

                # Extract attendees
                attendees = []
                for att in component.get("attendee", []):
                    if hasattr(att, "params"):
                        cn = att.params.get("CN", "")
                        email = str(att).replace("mailto:", "")
                        if cn:
                            attendees.append(f"{cn} <{email}>")
                        else:
                            attendees.append(email)

                # Get organizer
                organizer = ""
                org = component.get("organizer")
                if org:
                    organizer = str(org).replace("mailto:", "")

                events.append(CalendarEvent(
                    event_id=str(component.get("uid", "")),
                    title=str(component.get("summary", "")),
                    start=event_start,
                    end=event_end,
                    organizer=organizer,
                    attendees=attendees,
                    description=str(component.get("description", "") or ""),
                    location=str(component.get("location", "") or ""),
                    is_recurring=component.get("rrule") is not None,
                    provider="ical",
                    calendar_name=self.ics_path.stem,
                ))

            return events

        except ImportError:
            # Parse manually (basic support)
            return self._parse_ics_manually(start, end)
        except Exception:
            return []

    def _parse_ics_manually(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """Basic ICS parsing without icalendar library."""
        try:
            content = self.ics_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []

        events = []
        current_event: dict = {}
        in_event = False

        for line in content.split("\n"):
            line = line.strip()

            if line == "BEGIN:VEVENT":
                in_event = True
                current_event = {}
            elif line == "END:VEVENT":
                if current_event.get("dtstart"):
                    # Parse datetime
                    try:
                        dtstart = current_event["dtstart"]
                        # Handle various date formats
                        if "T" in dtstart:
                            if dtstart.endswith("Z"):
                                event_start = datetime.strptime(
                                    dtstart, "%Y%m%dT%H%M%SZ"
                                ).replace(tzinfo=UTC)
                            else:
                                event_start = datetime.strptime(
                                    dtstart[:15], "%Y%m%dT%H%M%S"
                                ).replace(tzinfo=UTC)
                        else:
                            event_start = datetime.strptime(
                                dtstart[:8], "%Y%m%d"
                            ).replace(tzinfo=UTC)

                        event_end = event_start + timedelta(hours=1)

                        if event_end >= start and event_start <= end:
                            events.append(CalendarEvent(
                                event_id=current_event.get("uid", ""),
                                title=current_event.get("summary", ""),
                                start=event_start,
                                end=event_end,
                                organizer=current_event.get("organizer", "").replace("mailto:", ""),
                                description=current_event.get("description", ""),
                                location=current_event.get("location", ""),
                                provider="ical",
                            ))
                    except (ValueError, KeyError):
                        pass
                in_event = False
            elif in_event and ":" in line:
                key, _, value = line.partition(":")
                # Handle parameters like DTSTART;TZID=...
                key = key.split(";")[0].lower()
                current_event[key] = value

        return events


class GoogleCalendarProvider(CalendarProvider):
    """Fetch events from Google Calendar via API."""

    def __init__(self):
        self.credentials_path = CONFIG_DIR / "google_calendar_credentials.json"
        self.token_path = CONFIG_DIR / "google_calendar_token.json"
        self._service = None

    def is_available(self) -> bool:
        # Check for credentials file or environment variable
        if self.credentials_path.exists():
            return True
        if os.environ.get("GOOGLE_CALENDAR_CREDENTIALS"):
            return True
        return False

    def _get_service(self):
        """Get authenticated Google Calendar service."""
        if self._service:
            return self._service

        try:
            from google.oauth2.credentials import Credentials  # type: ignore[import-untyped]
            from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]
            from googleapiclient.discovery import build  # type: ignore[import-untyped]

            SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

            creds = None
            if self.token_path.exists():
                creds = Credentials.from_authorized_user_file(
                    str(self.token_path), SCOPES
                )

            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    from google.auth.transport.requests import Request
                    creds.refresh(Request())
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        str(self.credentials_path), SCOPES
                    )
                    creds = flow.run_local_server(port=0)

                self.token_path.write_text(creds.to_json())

            self._service = build("calendar", "v3", credentials=creds)
            return self._service

        except ImportError:
            return None
        except Exception:
            return None

    def get_events(
        self,
        start: datetime,
        end: datetime,
        calendar_id: str | None = None,
    ) -> list[CalendarEvent]:
        service = self._get_service()
        if not service:
            return []

        calendar_id = calendar_id or "primary"

        try:
            events_result = service.events().list(
                calendarId=calendar_id,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute()

            events = []
            for item in events_result.get("items", []):
                # Parse start/end
                start_data = item.get("start", {})
                end_data = item.get("end", {})

                start_str = start_data.get("dateTime") or start_data.get("date")
                end_str = end_data.get("dateTime") or end_data.get("date")

                if not start_str:
                    continue

                event_start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                event_end = datetime.fromisoformat(end_str.replace("Z", "+00:00")) if end_str else event_start + timedelta(hours=1)

                # Extract attendees
                attendees = []
                for att in item.get("attendees", []):
                    name = att.get("displayName", "")
                    email = att.get("email", "")
                    if name and email:
                        attendees.append(f"{name} <{email}>")
                    elif email:
                        attendees.append(email)

                # Get organizer
                org = item.get("organizer", {})
                organizer = org.get("email", "")

                # Check for conference link
                meeting_link = ""
                conf = item.get("conferenceData", {})
                for entry in conf.get("entryPoints", []):
                    if entry.get("entryPointType") == "video":
                        meeting_link = entry.get("uri", "")
                        break

                # Check for attachments
                attached_docs = []
                for att in item.get("attachments", []):
                    attached_docs.append(att.get("fileUrl", ""))

                events.append(CalendarEvent(
                    event_id=item.get("id", ""),
                    title=item.get("summary", ""),
                    start=event_start,
                    end=event_end,
                    organizer=organizer,
                    attendees=attendees,
                    description=item.get("description", ""),
                    location=item.get("location", ""),
                    is_recurring=item.get("recurringEventId") is not None,
                    recurrence_name=item.get("summary", "") if item.get("recurringEventId") else "",
                    meeting_link=meeting_link,
                    attached_docs=attached_docs,
                    provider="google",
                    calendar_name=calendar_id,
                ))

            return events

        except Exception:
            return []


class OutlookCalendarProvider(CalendarProvider):
    """Fetch events from Outlook/Teams calendar via Microsoft Graph API.

    Supports:
    - Personal Microsoft accounts
    - Work/school (Azure AD) accounts
    - Teams meeting detection

    Setup:
    1. Register an app at https://portal.azure.com
    2. Add Calendar.Read permission
    3. Set MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET env vars
       OR create config/microsoft_calendar_credentials.json
    """

    def __init__(self):
        self.credentials_path = CONFIG_DIR / "microsoft_calendar_credentials.json"
        self.token_path = CONFIG_DIR / "microsoft_calendar_token.json"
        self._access_token: str | None = None
        self._token_expiry: datetime | None = None

    def is_available(self) -> bool:
        # Check for credentials file or environment variables
        if self.credentials_path.exists():
            return True
        if os.environ.get("MICROSOFT_CLIENT_ID"):
            return True
        return False

    def _get_credentials(self) -> tuple[str, str]:
        """Get client ID and secret."""
        # Try environment variables first
        client_id = os.environ.get("MICROSOFT_CLIENT_ID")
        client_secret = os.environ.get("MICROSOFT_CLIENT_SECRET")

        if client_id and client_secret:
            return (client_id, client_secret)

        # Try credentials file
        if self.credentials_path.exists():
            try:
                creds = json.loads(self.credentials_path.read_text())
                return (creds["client_id"], creds["client_secret"])
            except (json.JSONDecodeError, KeyError):
                pass

        raise RuntimeError("Microsoft credentials not configured")

    def _get_access_token(self) -> str:
        """Get or refresh access token."""
        # Check if we have a valid cached token
        if self._access_token is not None and self._token_expiry is not None:
            if datetime.now(UTC) < self._token_expiry:
                return self._access_token  # str is guaranteed here

        # Try to load from token file
        if self.token_path.exists():
            try:
                token_data = json.loads(self.token_path.read_text())
                expiry = datetime.fromisoformat(token_data["expires_at"])
                if datetime.now(UTC) < expiry:
                    self._access_token = token_data["access_token"]
                    self._token_expiry = expiry
                    assert self._access_token is not None
                    return self._access_token

                # Try refresh
                if token_data.get("refresh_token"):
                    return self._refresh_token(token_data["refresh_token"])
            except (json.JSONDecodeError, KeyError):
                pass

        # Need interactive auth
        return self._interactive_auth()

    def _refresh_token(self, refresh_token: str) -> str:
        """Refresh the access token."""
        import urllib.parse
        import urllib.request

        client_id, client_secret = self._get_credentials()

        data = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": "Calendars.Read offline_access",
        }).encode()

        req = urllib.request.Request(
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            data=data,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())

                self._access_token = result["access_token"]
                expires_in = result.get("expires_in", 3600)
                self._token_expiry = datetime.now(UTC) + timedelta(seconds=expires_in - 60)

                # Save token
                token_data = {
                    "access_token": self._access_token,
                    "refresh_token": result.get("refresh_token", refresh_token),
                    "expires_at": self._token_expiry.isoformat(),
                }
                atomic_write(self.token_path, token_data)

                assert self._access_token is not None
                return self._access_token
        except Exception:
            # Refresh failed, need interactive auth
            return self._interactive_auth()

    def _interactive_auth(self) -> str:
        """Perform interactive OAuth2 authentication."""
        # For now, use device code flow (works without redirect URI)
        import urllib.error
        import urllib.parse
        import urllib.request

        client_id, _ = self._get_credentials()

        # Request device code
        data = urllib.parse.urlencode({
            "client_id": client_id,
            "scope": "Calendars.Read offline_access",
        }).encode()

        req = urllib.request.Request(
            "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode",
            data=data,
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())

        # Show user message
        print("\n  To authenticate with Microsoft:")
        print(f"  1. Go to: {result['verification_uri']}")
        print(f"  2. Enter code: {result['user_code']}")
        print("  Waiting for authentication...\n")

        # Poll for token
        device_code = result["device_code"]
        interval = result.get("interval", 5)

        client_id, client_secret = self._get_credentials()

        while True:
            import time
            time.sleep(interval)

            poll_data = urllib.parse.urlencode({
                "client_id": client_id,
                "client_secret": client_secret,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            }).encode()

            poll_req = urllib.request.Request(
                "https://login.microsoftonline.com/common/oauth2/v2.0/token",
                data=poll_data,
                method="POST",
            )

            try:
                with urllib.request.urlopen(poll_req, timeout=30) as resp:
                    token_result = json.loads(resp.read().decode())

                    self._access_token = token_result["access_token"]
                    expires_in = token_result.get("expires_in", 3600)
                    self._token_expiry = datetime.now(UTC) + timedelta(seconds=expires_in - 60)

                    # Save token
                    token_data = {
                        "access_token": self._access_token,
                        "refresh_token": token_result.get("refresh_token"),
                        "expires_at": self._token_expiry.isoformat(),
                    }
                    self.token_path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write(self.token_path, token_data)

                    print("  ✓ Microsoft authentication successful\n")
                    assert self._access_token is not None
                    return self._access_token

            except urllib.error.HTTPError as e:
                error_body = json.loads(e.read().decode())
                error_code = error_body.get("error")

                if error_code == "authorization_pending":
                    continue
                if error_code == "slow_down":
                    interval += 5
                    continue
                raise RuntimeError(f"Auth failed: {error_body.get('error_description', error_code)}")

    def get_events(
        self,
        start: datetime,
        end: datetime,
        calendar_id: str | None = None,
    ) -> list[CalendarEvent]:
        """Fetch events from Microsoft Graph API."""
        if not self.is_available():
            return []

        try:
            import urllib.parse
            import urllib.request

            access_token = self._get_access_token()

            # Build Graph API request
            start_iso = start.strftime("%Y-%m-%dT%H:%M:%S.0000000")
            end_iso = end.strftime("%Y-%m-%dT%H:%M:%S.0000000")

            # calendarView gives us expanded recurring events
            url = (
                "https://graph.microsoft.com/v1.0/me/calendarView"
                f"?startDateTime={urllib.parse.quote(start_iso)}"
                f"&endDateTime={urllib.parse.quote(end_iso)}"
                "&$select=id,subject,start,end,organizer,attendees,bodyPreview,location,isOnlineMeeting,onlineMeeting,seriesMasterId"
                "&$orderby=start/dateTime"
                "&$top=50"
            )

            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            })

            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())

            events = []
            for item in data.get("value", []):
                # Parse start/end
                start_data = item.get("start", {})
                end_data = item.get("end", {})

                start_str = start_data.get("dateTime", "")
                end_str = end_data.get("dateTime", "")
                tz = start_data.get("timeZone", "UTC")

                if not start_str:
                    continue

                # Graph returns times without timezone, add Z for UTC
                try:
                    event_start = datetime.fromisoformat(start_str.replace("Z", ""))
                    if tz == "UTC":
                        event_start = event_start.replace(tzinfo=UTC)
                    else:
                        event_start = event_start.replace(tzinfo=UTC)  # Simplified

                    event_end = datetime.fromisoformat(end_str.replace("Z", "")) if end_str else event_start + timedelta(hours=1)
                    if tz == "UTC":
                        event_end = event_end.replace(tzinfo=UTC)
                    else:
                        event_end = event_end.replace(tzinfo=UTC)
                except ValueError:
                    continue

                # Extract attendees
                attendees = []
                for att in item.get("attendees", []):
                    email_addr = att.get("emailAddress", {})
                    name = email_addr.get("name", "")
                    email = email_addr.get("address", "")
                    if name and email:
                        attendees.append(f"{name} <{email}>")
                    elif email:
                        attendees.append(email)

                # Get organizer
                org = item.get("organizer", {}).get("emailAddress", {})
                organizer = org.get("address", "")

                # Check for Teams meeting link
                meeting_link = ""
                if item.get("isOnlineMeeting"):
                    online = item.get("onlineMeeting", {})
                    meeting_link = online.get("joinUrl", "")

                # Check location for meeting links
                location = item.get("location", {}).get("displayName", "")
                if not meeting_link and location:
                    # Sometimes Teams link is in location
                    if "teams.microsoft.com" in location.lower():
                        meeting_link = location

                events.append(CalendarEvent(
                    event_id=item.get("id", ""),
                    title=item.get("subject", ""),
                    start=event_start,
                    end=event_end,
                    organizer=organizer,
                    attendees=attendees,
                    description=item.get("bodyPreview", ""),
                    location=location,
                    is_recurring=item.get("seriesMasterId") is not None,
                    recurrence_name=item.get("subject", "") if item.get("seriesMasterId") else "",
                    meeting_link=meeting_link,
                    provider="outlook",
                    calendar_name="primary",
                ))

            return events

        except Exception as e:
            # Log but don't fail
            print(f"Warning: Outlook calendar fetch failed: {e}")
            return []


class CalendarContext:
    """Main interface for calendar correlation."""

    def __init__(self):
        self.providers: list[CalendarProvider] = []
        self._cache: dict[str, CalendarEvent] = {}

        # Register available providers
        outlook = OutlookCalendarProvider()
        if outlook.is_available():
            self.providers.append(outlook)

        google = GoogleCalendarProvider()
        if google.is_available():
            self.providers.append(google)

        ical = ICalFileProvider()
        if ical.is_available():
            self.providers.append(ical)

        self._load_cache()

    def _load_cache(self) -> None:
        """Load cached events from disk."""
        if CALENDAR_CACHE.exists():
            try:
                data = json.loads(CALENDAR_CACHE.read_text())
                for item in data:
                    event = CalendarEvent.from_dict(item)
                    self._cache[event.event_id] = event
            except (json.JSONDecodeError, OSError):
                pass

    def _save_cache(self) -> None:
        """Persist cache to disk."""
        CALENDAR_CACHE.parent.mkdir(parents=True, exist_ok=True)
        data = [e.to_dict() for e in self._cache.values()]
        atomic_write(CALENDAR_CACHE, data)

    def find_event_at(
        self,
        timestamp: str | datetime,
        window_minutes: int = 15,
    ) -> CalendarEvent | None:
        """Find a calendar event that overlaps with the given timestamp.

        Args:
            timestamp: ISO timestamp or datetime
            window_minutes: How far after an event ends to still consider it
                           (captures "voice memo recorded right after meeting")

        Returns:
            Best matching CalendarEvent, or None
        """
        if isinstance(timestamp, str):
            try:
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            dt = timestamp

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)

        # Search window: 2 hours before to window_minutes after
        search_start = dt - timedelta(hours=2)
        search_end = dt + timedelta(minutes=window_minutes)

        candidates: list[CalendarEvent] = []

        for provider in self.providers:
            try:
                events = provider.get_events(search_start, search_end)
                for event in events:
                    # Check if timestamp falls within event (or shortly after)
                    event_end_with_buffer = event.end + timedelta(minutes=window_minutes)
                    if event.start <= dt <= event_end_with_buffer:
                        candidates.append(event)
                        self._cache[event.event_id] = event
            except Exception:
                continue

        if not candidates:
            return None

        # Prefer events that are still ongoing
        ongoing = [e for e in candidates if e.start <= dt <= e.end]
        if ongoing:
            # Return the one that started most recently
            return max(ongoing, key=lambda e: e.start)

        # Otherwise, return the event that ended most recently
        return max(candidates, key=lambda e: e.end)

    def get_attendees_for_signal(
        self,
        timestamp: str | datetime,
        exclude_originator: str | None = None,
    ) -> list[str]:
        """Get meeting attendees who might want to see this signal.

        Args:
            timestamp: When the signal was captured
            exclude_originator: Email to exclude (the person who created the signal)

        Returns:
            List of attendee emails
        """
        event = self.find_event_at(timestamp)
        if not event:
            return []

        emails = event.attendee_emails()

        # Add organizer if not in attendees
        if event.organizer and "@" in event.organizer:
            org_email = event.organizer.lower()
            if org_email not in emails:
                emails.append(org_email)

        # Exclude originator
        if exclude_originator:
            exclude_lower = exclude_originator.lower()
            emails = [e for e in emails if e != exclude_lower]

        return emails

    def enrich_signal_metadata(
        self,
        signal,
        window_minutes: int = 15,
    ) -> bool:
        """Add calendar context to a signal's metadata.

        Returns True if a matching event was found.
        """
        event = self.find_event_at(signal.timestamp, window_minutes)
        if not event:
            return False

        signal.metadata["calendar_event"] = event.to_dict()
        signal.metadata["meeting_title"] = event.title
        signal.metadata["meeting_attendees"] = event.attendee_emails()

        if event.is_recurring:
            signal.metadata["recurring_meeting"] = event.recurrence_name or event.title

        return True

    def suggest_fyi_recipients(
        self,
        signal,
        exclude: list[str] | None = None,
    ) -> list[dict]:
        """Suggest people who should be notified about this signal.

        Returns list of suggestions with reasoning.
        """
        suggestions = []
        exclude = exclude or []
        exclude_lower = [e.lower() for e in exclude]

        # From calendar
        event = self.find_event_at(signal.timestamp)
        if event:
            for email in event.attendee_emails():
                if email.lower() not in exclude_lower:
                    suggestions.append({
                        "email": email,
                        "reason": f"Attended '{event.title}' when this signal was captured",
                        "source": "calendar",
                        "confidence": 0.8,
                    })

            if event.organizer and "@" in event.organizer:
                org_email = event.organizer.lower()
                if org_email not in exclude_lower and org_email not in [s["email"] for s in suggestions]:
                    suggestions.append({
                        "email": org_email,
                        "reason": f"Organized '{event.title}' - likely stakeholder",
                        "source": "calendar",
                        "confidence": 0.9,
                    })

        return suggestions

    def is_available(self) -> bool:
        """Check if any calendar provider is configured."""
        return len(self.providers) > 0

    def status(self) -> dict:
        """Get calendar context status."""
        return {
            "providers_available": len(self.providers),
            "provider_names": [type(p).__name__ for p in self.providers],
            "cached_events": len(self._cache),
        }


def get_calendar_context() -> CalendarContext:
    """Get singleton calendar context instance."""
    return CalendarContext()
