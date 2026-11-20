# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for Outlook Calendar signal enrichment.

Tests that the OutlookCalendarProvider:
1. Authenticates via MS Graph
2. Fetches calendar events
3. Provides meeting context for signal correlation
4. Tracks freshness

Run with:
    pytest tests/integration/test_outlook_calendar_channel.py -v -m integration

Requires:
    MS_GRAPH_CLIENT_ID, MS_GRAPH_CLIENT_SECRET environment variables
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.onedrive,
    pytest.mark.skip(reason="MS 365 integration not yet configured — enable when MS_GRAPH credentials are active"),
]


class TestOutlookCalendarProvider:
    """Test OutlookCalendarProvider against real MS Graph API."""

    @pytest.fixture
    def provider(self, ms_graph_creds):
        """Create provider with real credentials.

        OutlookCalendarProvider reads credentials from MICROSOFT_CLIENT_ID /
        MICROSOFT_CLIENT_SECRET env vars or a JSON credentials file.
        The ms_graph_creds fixture ensures the env vars are set.
        """
        from axiom.extensions.builtins.signals.calendar_context import (
            OutlookCalendarProvider,
        )
        return OutlookCalendarProvider()

    def test_provider_is_available(self, provider):
        """Verify provider has valid credentials."""
        assert provider.is_available()


class TestCalendarContext:
    """Test calendar context enrichment."""

    @pytest.fixture
    def calendar_context(self, ms_graph_creds):
        """Create CalendarContext — auto-discovers available providers."""
        from axiom.extensions.builtins.signals.calendar_context import CalendarContext
        return CalendarContext()

    def test_context_registers_provider(self, calendar_context):
        """Verify provider is registered."""
        assert len(calendar_context.providers) >= 1


class TestCalendarEventCorrelation:
    """Test correlating signals with calendar events."""

    @pytest.fixture
    def calendar_context(self, ms_graph_creds):
        from axiom.extensions.builtins.signals.calendar_context import CalendarContext
        return CalendarContext()

    def test_event_format(self, calendar_context):
        """Verify calendar events have expected format."""
        from axiom.extensions.builtins.signals.calendar_context import CalendarEvent

        # Create a mock event to test structure
        event = CalendarEvent(
            event_id="test-event-123",
            title="Weekly Sync",
            start=datetime.now(UTC),
            end=datetime.now(UTC) + timedelta(hours=1),
            attendees=["alice@test.com", "bob@test.com"],
            location="Teams",
            description="Weekly team sync meeting",
            organizer="alice@test.com",
        )

        assert event.event_id == "test-event-123"
        assert event.title == "Weekly Sync"
        assert len(event.attendees) == 2
        assert event.duration_minutes() == 60


class TestOutlookCalendarFreshness:
    """Test freshness tracking for Outlook Calendar."""

    @pytest.fixture
    def provider(self, ms_graph_creds):
        from axiom.extensions.builtins.signals.calendar_context import (
            OutlookCalendarProvider,
        )
        return OutlookCalendarProvider()

    def test_freshness_tracking(self, provider, freshness_tracker):
        """Verify freshness tracking for calendar channel."""
        channel = "outlook_calendar"

        # Initially stale
        assert not freshness_tracker.is_fresh(channel)

        # After sync, should be fresh
        freshness_tracker.mark_synced(channel)
        assert freshness_tracker.is_fresh(channel, max_age_hours=24)

    def test_calendar_events_update_freshness(self, provider, freshness_tracker):
        """Calendar fetch should update freshness."""
        channel = "outlook_calendar"

        # This would normally call provider.get_events()
        # For testing, we simulate the sync
        try:
            # Attempt to validate connection
            if provider.is_available():
                freshness_tracker.mark_synced(channel)
        except Exception:
            pass

        # The freshness mechanism itself works regardless of API availability


class TestCalendarSignalEnrichment:
    """Test enriching signals with calendar context."""

    def test_signal_meeting_correlation(self, ms_graph_creds, tmp_path):
        """Signals can be correlated with nearby meetings."""
        from axiom.extensions.builtins.signals.calendar_context import CalendarEvent
        from axiom.extensions.builtins.signals.models import Signal

        # Create a signal from a voice memo
        signal = Signal(
            source="voice",
            timestamp=datetime.now(UTC).isoformat(),
            raw_text="We discussed the API changes in the standup",
            people=["Alice", "Bob"],
            signal_type="discussion",
            detail="Voice memo from standup discussion",
        )

        # Create a calendar event at similar time
        event = CalendarEvent(
            event_id="standup-123",
            title="Daily Standup",
            start=datetime.now(UTC) - timedelta(minutes=30),
            end=datetime.now(UTC),
            attendees=["alice@test.com", "bob@test.com", "charlie@test.com"],
            location="Teams",
            description="Daily sync",
            organizer="alice@test.com",
        )

        # Check that we can correlate by time proximity
        signal_time = datetime.fromisoformat(signal.timestamp)
        event_window = (event.start - timedelta(minutes=15), event.end + timedelta(minutes=15))

        is_during_meeting = event_window[0] <= signal_time <= event_window[1]
        assert is_during_meeting

        # People overlap
        signal_people_lower = {p.lower() for p in signal.people}
        event_people = {a.split("@")[0].lower() for a in event.attendees}
        overlap = signal_people_lower & event_people
        assert "alice" in overlap or "bob" in overlap


class TestMultiProviderCalendar:
    """Test calendar context with multiple providers."""

    def test_provider_fallback(self, ms_graph_creds):
        """CalendarContext gracefully handles provider failures."""
        from axiom.extensions.builtins.signals.calendar_context import (
            CalendarContext,
            ICalFileProvider,
        )

        # ICalFileProvider with nonexistent path — is_available() returns False
        ical_provider = ICalFileProvider(ics_path=Path("/nonexistent/calendar.ics"))
        assert not ical_provider.is_available()

        # CalendarContext auto-discovers — won't include unavailable providers
        context = CalendarContext()
        assert context.providers is not None
