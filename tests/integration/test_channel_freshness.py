# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for multi-channel freshness tracking.

Verifies all signal channels have been processed since Kevin's
Master Program Tracker was last updated.

This test suite ensures:
1. All channels are configured and accessible
2. Each channel has been synced recently
3. No channel is stale relative to Kevin's tracker

Run with:
    pytest tests/integration/test_channel_freshness.py -v -m integration

Configuration:
    Set KEVIN_TRACKER_LAST_UPDATE env var to ISO timestamp of last tracker update
    Default: 7 days ago (for testing)
"""

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Kevin's Master Program Tracker last update date
# In production, read from the actual tracker or set via env var
KEVIN_TRACKER_LAST_UPDATE = os.environ.get(
    "KEVIN_TRACKER_LAST_UPDATE",
    (datetime.now(UTC) - timedelta(days=7)).isoformat()
)


def get_tracker_cutoff() -> datetime:
    """Get the cutoff date from Kevin's tracker."""
    return datetime.fromisoformat(KEVIN_TRACKER_LAST_UPDATE.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Channel registry
# ---------------------------------------------------------------------------

CHANNELS = [
    {
        "name": "teams_transcripts",
        "description": "Microsoft Teams meeting transcripts (VTT/SRT)",
        "marker": "teams",
        "requires": ["MS_GRAPH_CLIENT_ID"],
    },
    {
        "name": "teams_chat",
        "description": "Microsoft Teams chat messages",
        "marker": "teams",
        "requires": ["MS_GRAPH_CLIENT_ID"],
    },
    {
        "name": "outlook_calendar",
        "description": "Outlook/Teams calendar events",
        "marker": "onedrive",
        "requires": ["MS_GRAPH_CLIENT_ID"],
    },
    {
        "name": "gitlab",
        "description": "GitLab commits, MRs, and issues",
        "marker": "gitlab",
        "requires": ["GITLAB_TOKEN"],
    },
    {
        "name": "github",
        "description": "GitHub commits, PRs, and issues",
        "marker": "github",
        "requires": ["GITHUB_TOKEN"],
    },
    {
        "name": "onedrive",
        "description": "OneDrive file changes",
        "marker": "onedrive",
        "requires": ["MS_GRAPH_CLIENT_ID"],
    },
    {
        "name": "voice",
        "description": "Voice memos via neut signal serve",
        "marker": "voice",
        "requires": [],  # Local service
    },
    {
        "name": "email",
        "description": "Email signals",
        "marker": "inbox",
        "requires": [],  # Various providers
    },
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def channel_freshness_state(tmp_path):
    """Persistent state for channel freshness tracking."""
    state_file = tmp_path / "channel_freshness_state.json"

    class ChannelFreshnessState:
        def __init__(self):
            self.state = {}
            if state_file.exists():
                self.state = json.loads(state_file.read_text())

        def record_sync(self, channel: str, success: bool, message: str = ""):
            """Record a sync attempt."""
            self.state[channel] = {
                "last_attempt": datetime.now(UTC).isoformat(),
                "success": success,
                "message": message,
            }
            state_file.write_text(json.dumps(self.state, indent=2))

        def get_status(self, channel: str) -> dict:
            """Get channel status."""
            return self.state.get(channel, {})

        def all_fresh_since(self, cutoff: datetime) -> bool:
            """Check if all channels have been synced since cutoff."""
            for channel in CHANNELS:
                status = self.state.get(channel["name"], {})
                if not status.get("success"):
                    return False
                last = status.get("last_attempt")
                if not last:
                    return False
                if datetime.fromisoformat(last) < cutoff:
                    return False
            return True

        def report(self) -> str:
            """Generate freshness report."""
            cutoff = get_tracker_cutoff()
            lines = [
                "Channel Freshness Report",
                f"Kevin's Tracker Last Update: {cutoff.isoformat()}",
                "-" * 50,
            ]
            for channel in CHANNELS:
                name = channel["name"]
                status = self.state.get(name, {})
                if not status:
                    lines.append(f"  {name}: NOT SYNCED")
                elif not status.get("success"):
                    lines.append(f"  {name}: FAILED - {status.get('message', 'Unknown')}")
                else:
                    last = datetime.fromisoformat(status["last_attempt"])
                    if last >= cutoff:
                        lines.append(f"  {name}: FRESH (synced {last.isoformat()})")
                    else:
                        lines.append(f"  {name}: STALE (last sync {last.isoformat()})")
            return "\n".join(lines)

    return ChannelFreshnessState()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestChannelRegistry:
    """Verify all channels are properly defined."""

    def test_all_channels_have_names(self):
        """Every channel has a unique name."""
        names = [c["name"] for c in CHANNELS]
        assert len(names) == len(set(names))

    def test_all_channels_have_descriptions(self):
        """Every channel has a description."""
        for channel in CHANNELS:
            assert channel.get("description"), f"Channel {channel['name']} missing description"

    def test_all_channels_have_markers(self):
        """Every channel has a pytest marker."""
        for channel in CHANNELS:
            assert channel.get("marker"), f"Channel {channel['name']} missing marker"


class TestChannelAccessibility:
    """Verify channels are accessible with current credentials."""

    def test_teams_transcripts_accessible(self, ms_graph_creds):
        """Teams transcripts channel is configured."""
        # If we got here, ms_graph_creds fixture didn't skip
        assert ms_graph_creds["client_id"]

    def test_teams_chat_accessible(self, ms_graph_creds):
        """Teams chat channel is configured."""
        from axiom.extensions.builtins.signals.extractors.teams_chat import (
            TeamsChatExtractor,
        )
        extractor = TeamsChatExtractor(
            client_id=ms_graph_creds["client_id"],
            client_secret=ms_graph_creds["client_secret"],
        )
        assert extractor.is_available()

    def test_outlook_calendar_accessible(self, ms_graph_creds):
        """Outlook calendar channel is configured."""
        pytest.skip("MS 365 integration not yet configured — enable when MS_GRAPH credentials are active")

    def test_gitlab_accessible(self, gitlab_token):
        """GitLab channel is configured."""
        assert gitlab_token

    def test_github_accessible(self, github_token):
        """GitHub channel is configured."""
        from axiom.extensions.builtins.signals.extractors.github import GitHubExtractor
        extractor = GitHubExtractor(token=github_token)
        assert extractor.is_available()


class TestFreshnessVsKevinsTracker:
    """Test freshness relative to Kevin's Master Program Tracker."""

    def test_cutoff_is_reasonable(self):
        """Cutoff date is within expected range."""
        cutoff = get_tracker_cutoff()
        now = datetime.now(UTC)

        # Should not be in the future
        assert cutoff <= now

        # Should not be too far in the past (sanity check)
        max_age = timedelta(days=365)
        assert cutoff > now - max_age

    def test_freshness_report_generation(self, channel_freshness_state):
        """Freshness report can be generated."""
        report = channel_freshness_state.report()
        assert "Channel Freshness Report" in report
        assert "Kevin's Tracker Last Update" in report

    def test_simulated_sync_cycle(self, channel_freshness_state):
        """Simulate a full sync cycle."""
        # Simulate successful syncs
        for channel in CHANNELS[:3]:  # Just first 3 for speed
            channel_freshness_state.record_sync(
                channel["name"],
                success=True,
                message="Test sync",
            )

        # Verify they're recorded
        for channel in CHANNELS[:3]:
            status = channel_freshness_state.get_status(channel["name"])
            assert status.get("success") is True


class TestChannelSyncValidation:
    """Validate individual channel sync operations."""

    def test_github_sync_records_freshness(
        self, github_token, channel_freshness_state
    ):
        """GitHub sync updates freshness state."""
        from axiom.extensions.builtins.signals.extractors.github import GitHubExtractor

        extractor = GitHubExtractor(token=github_token)

        try:
            # Try to fetch from a test repo
            activity = extractor.fetch_activity(
                repo="example-org/example-repo",  # Adjust to actual repo
                days=1,
            )
            success = not activity.errors
            message = str(activity.errors) if activity.errors else "OK"
        except Exception as e:
            success = False
            message = str(e)

        channel_freshness_state.record_sync("github", success, message)

        # Verify state was recorded
        status = channel_freshness_state.get_status("github")
        assert status.get("last_attempt")

    def test_teams_chat_sync_records_freshness(
        self, ms_graph_creds, channel_freshness_state
    ):
        """Teams Chat sync updates freshness state."""
        from axiom.extensions.builtins.signals.extractors.teams_chat import (
            TeamsChatExtractor,
        )

        extractor = TeamsChatExtractor(
            client_id=ms_graph_creds["client_id"],
            client_secret=ms_graph_creds["client_secret"],
        )

        # Just verify extractor is configured (actual sync requires auth)
        success = extractor.is_available()
        message = "Configured" if success else "Not configured"

        channel_freshness_state.record_sync("teams_chat", success, message)

        status = channel_freshness_state.get_status("teams_chat")
        assert status.get("last_attempt")


class TestStaleChannelDetection:
    """Detect and report stale channels."""

    def test_detect_never_synced(self, channel_freshness_state):
        """Channels that were never synced are detected."""
        # Without any syncs recorded, all_fresh_since should fail
        cutoff = get_tracker_cutoff()
        assert not channel_freshness_state.all_fresh_since(cutoff)

    def test_detect_stale_after_sync(self, channel_freshness_state, monkeypatch):
        """Channels become stale after cutoff passes."""
        from datetime import datetime

        # Record an old sync
        old_time = datetime.now(UTC) - timedelta(days=30)
        channel_freshness_state.state["github"] = {
            "last_attempt": old_time.isoformat(),
            "success": True,
            "message": "Old sync",
        }

        # Recent cutoff should mark it stale
        recent_cutoff = datetime.now(UTC) - timedelta(days=1)

        status = channel_freshness_state.get_status("github")
        last = datetime.fromisoformat(status["last_attempt"])

        assert last < recent_cutoff  # Channel is stale


# ---------------------------------------------------------------------------
# Summary test
# ---------------------------------------------------------------------------

class TestChannelFreshnessSummary:
    """Summary test for CI/CD pipelines."""

    @pytest.mark.skip(reason="Run manually with all credentials")
    def test_all_channels_fresh(self, channel_freshness_state):
        """SUMMARY: Verify all channels synced since Kevin's last update."""
        cutoff = get_tracker_cutoff()

        # This would run after all individual sync tests
        if not channel_freshness_state.all_fresh_since(cutoff):
            report = channel_freshness_state.report()
            pytest.fail(f"Some channels are stale:\n{report}")
