# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Calendar event extractor for Sense pipeline.

Extracts signals from calendar events (Google Calendar API or .ics files).
Identifies meetings, deadlines, and scheduling patterns relevant to PRDs.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..models import Extraction, Signal


@dataclass
class CalendarEvent:
    """Parsed calendar event."""
    event_id: str
    title: str
    start: datetime
    end: datetime
    attendees: list[str]
    description: str | None
    location: str | None
    recurring: bool
    source_file: str | None  # For .ics imports


class CalendarExtractor:
    """Extracts signals from calendar events.

    Supports:
    - Google Calendar API (requires credentials)
    - iCalendar (.ics) file import

    Usage:
        extractor = CalendarExtractor()

        # From Google Calendar
        signals = extractor.extract_from_google(since=datetime(2026, 2, 10))

        # From .ics file
        signals = extractor.extract_from_ics(Path("inbox/raw/calendar/export.ics"))
    """

    # Keywords that indicate PRD relevance
    PRD_KEYWORDS = {
        "ops_log": ["ops", "operations", "console", "shift", "system", "compliance"],
        "experiment_manager": ["experiment", "sample", "testing", "roc", "tracking"],
        "operator_dashboard": ["operator", "dashboard", "alerts", "monitoring"],
        "researcher_dashboard": ["researcher", "results", "analysis", "data"],
    }

    # Known stakeholders for attribution
    STAKEHOLDERS = {
        "jim": "Jim (TJ)",
        "tj": "Jim (TJ)",
        "nick": "Nick Luciano",
        "luciano": "Nick Luciano",
        "khiloni": "Khiloni Shah",
        "kevin": "Kevin Clarno",
        "clarno": "Kevin Clarno",
        "ben": "Ben Booth",
    }

    def __init__(self, inbox_path: Path | None = None):
        """Initialize extractor.

        Args:
            inbox_path: Path to inbox directory. Defaults to tools/agents/inbox/
        """
        self.inbox_path = inbox_path or Path(__file__).parent.parent / "inbox"
        self.calendar_dir = self.inbox_path / "raw" / "calendar"

    def extract_from_google(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        calendar_id: str = "primary",
    ) -> Extraction:
        """Extract signals from Google Calendar API.

        Args:
            since: Start of date range (default: 14 days ago)
            until: End of date range (default: now)
            calendar_id: Google Calendar ID (default: primary)

        Returns:
            Extraction containing signals from calendar events
        """
        # TODO: Implement Google Calendar API integration
        # 1. Load credentials from ~/.config/neutron/google_credentials.json
        # 2. Build calendar service
        # 3. List events in date range
        # 4. Parse each event into CalendarEvent
        # 5. Extract signals from events
        raise NotImplementedError("Google Calendar API integration pending")

    def extract_from_ics(self, ics_path: Path) -> Extraction:
        """Extract signals from an iCalendar (.ics) file.

        Args:
            ics_path: Path to .ics file

        Returns:
            Extraction containing signals from calendar events
        """
        # TODO: Implement .ics parsing
        # 1. Parse .ics with icalendar library
        # 2. Extract VEVENT components
        # 3. Parse each into CalendarEvent
        # 4. Extract signals from events
        raise NotImplementedError(".ics parsing pending")

    def extract_all(self, since: datetime | None = None) -> Extraction:
        """Extract from all calendar sources in inbox.

        Args:
            since: Only process events after this date

        Returns:
            Combined extraction from all sources
        """
        signals = []

        # Process any .ics files in calendar directory
        if self.calendar_dir.exists():
            for ics_file in self.calendar_dir.glob("*.ics"):
                extraction = self.extract_from_ics(ics_file)
                signals.extend(extraction.signals)

        return Extraction(
            extractor="calendar",
            source_file=str(self.calendar_dir),
            signals=signals,
        )

    def _parse_events(self, raw_events: list[dict]) -> Iterator[CalendarEvent]:
        """Parse raw event data into CalendarEvent objects."""
        for event in raw_events:
            yield CalendarEvent(
                event_id=event.get("id", ""),
                title=event.get("summary", ""),
                start=self._parse_datetime(event.get("start", {})),
                end=self._parse_datetime(event.get("end", {})),
                attendees=self._extract_attendees(event.get("attendees", [])),
                description=event.get("description"),
                location=event.get("location"),
                recurring=bool(event.get("recurringEventId")),
                source_file=event.get("source_file"),
            )

    def _extract_signals_from_event(self, event: CalendarEvent) -> list[Signal]:
        """Extract signals from a single calendar event.

        Looks for:
        - Meeting titles indicating PRD discussions
        - Attendees who are known stakeholders
        - Description containing requirements/decisions
        """
        signals = []

        # Determine PRD target from title/description
        prd_target = self._infer_prd_target(event.title, event.description or "")

        # Extract mentioned people
        people = self._extract_people(event.attendees, event.description or "")

        # Create signal for the meeting itself
        if prd_target or people:
            signals.append(Signal(
                source="calendar",
                timestamp=event.start.isoformat(),
                raw_text=self._format_event_text(event),
                signal_type="insight",  # Meetings are context/insight
                initiatives=[prd_target] if prd_target else [],
                people=people,
                detail=f"Meeting: {event.title}",
                confidence=0.6,  # Calendar events are lower confidence
                metadata={
                    "event_id": event.event_id,
                    "attendees": event.attendees,
                    "duration_minutes": self._calc_duration(event),
                },
            ))

        return signals

    def _infer_prd_target(self, title: str, description: str) -> str | None:
        """Infer PRD target from event title and description."""
        text = f"{title} {description}".lower()

        for prd, keywords in self.PRD_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                return prd

        return None

    def _extract_people(self, attendees: list[str], description: str) -> list[str]:
        """Extract known stakeholders from attendees and description."""
        people = []
        combined = " ".join(attendees) + " " + description
        combined_lower = combined.lower()

        for key, name in self.STAKEHOLDERS.items():
            if key in combined_lower and name not in people:
                people.append(name)

        return people

    def _format_event_text(self, event: CalendarEvent) -> str:
        """Format event as text for signal raw_text."""
        parts = [
            f"Meeting: {event.title}",
            f"Date: {event.start.strftime('%Y-%m-%d %H:%M')}",
            f"Attendees: {', '.join(event.attendees)}",
        ]
        if event.description:
            parts.append(f"Description: {event.description}")
        return "\n".join(parts)

    def _parse_datetime(self, dt_dict: dict) -> datetime:
        """Parse datetime from Google Calendar format."""
        if "dateTime" in dt_dict:
            return datetime.fromisoformat(dt_dict["dateTime"].replace("Z", "+00:00"))
        elif "date" in dt_dict:
            return datetime.fromisoformat(dt_dict["date"])
        return datetime.now()

    def _extract_attendees(self, attendees: list[dict]) -> list[str]:
        """Extract attendee emails/names."""
        return [a.get("email", a.get("displayName", "")) for a in attendees]

    def _calc_duration(self, event: CalendarEvent) -> int:
        """Calculate event duration in minutes."""
        return int((event.end - event.start).total_seconds() / 60)
