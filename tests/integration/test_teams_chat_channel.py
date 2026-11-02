# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for Teams Chat signal extraction.

Tests that the Teams Chat extractor:
1. Authenticates via MS Graph (device code flow)
2. Fetches messages from channels and chats
3. Extracts signals with proper types
4. Handles @mentions and reactions

Run with:
    pytest tests/integration/test_teams_chat_channel.py -v -m integration

Requires:
    MS_GRAPH_CLIENT_ID, MS_GRAPH_CLIENT_SECRET environment variables
"""

import json
from datetime import UTC, datetime

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.teams,
    pytest.mark.skip(reason="MS 365 integration not yet configured — enable when MS_GRAPH credentials are active"),
]


class TestTeamsChatExtractor:
    """Test TeamsChatExtractor against real MS Graph API."""

    @pytest.fixture
    def extractor(self, ms_graph_creds):
        """Create extractor with real credentials."""
        from axiom.extensions.builtins.signals.extractors.teams_chat import (
            TeamsChatExtractor,
        )
        return TeamsChatExtractor(
            client_id=ms_graph_creds["client_id"],
            client_secret=ms_graph_creds["client_secret"],
            tenant_id=ms_graph_creds["tenant_id"],
        )

    def test_extractor_is_available(self, extractor):
        """Verify extractor has valid credentials configured."""
        assert extractor.is_available()

    def test_can_handle_teams_chat_json(self, extractor, tmp_path):
        """Verify file pattern matching."""
        chat_file = tmp_path / "teams_chat_20250101.json"
        chat_file.write_text("{}")
        assert extractor.can_handle(chat_file)

        other_file = tmp_path / "random.json"
        other_file.write_text("{}")
        assert not extractor.can_handle(other_file)


class TestTeamsChatExtraction:
    """Test signal extraction from Teams chat exports."""

    @pytest.fixture
    def extractor(self, ms_graph_creds):
        from axiom.extensions.builtins.signals.extractors.teams_chat import (
            TeamsChatExtractor,
        )
        return TeamsChatExtractor(
            client_id=ms_graph_creds["client_id"],
            client_secret=ms_graph_creds["client_secret"],
            tenant_id=ms_graph_creds["tenant_id"],
        )

    def test_extract_action_items(self, extractor, tmp_path):
        """Messages with action requests become action_item signals."""
        export = {
            "exported_at": datetime.now(UTC).isoformat(),
            "time_window_days": 7,
            "messages": [
                {
                    "id": "msg1",
                    "content": "<p>Can you review the PR?</p>",
                    "content_text": "Can you review the PR?",
                    "sender": "Alice Smith",
                    "sender_email": "alice@test.com",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "team_name": "Project Team",
                    "channel_name": "General",
                    "chat_type": "channel",
                    "mentions": ["Bob Jones"],
                    "reactions": [],
                    "importance": "normal",
                    "has_attachments": False,
                    "reply_to_id": None,
                }
            ],
            "channels": [],
            "errors": [],
        }

        export_path = tmp_path / "teams_chat_action.json"
        export_path.write_text(json.dumps(export))

        extraction = extractor.extract(export_path)

        assert len(extraction.signals) == 1
        signal = extraction.signals[0]
        assert signal.signal_type == "action_item"
        assert "Alice Smith" in signal.people
        assert "Bob Jones" in signal.people  # mentioned

    def test_extract_blockers(self, extractor, tmp_path):
        """Messages mentioning blockers get blocker type."""
        export = {
            "exported_at": datetime.now(UTC).isoformat(),
            "time_window_days": 7,
            "messages": [
                {
                    "id": "msg2",
                    "content": "<p>I'm blocked waiting on the API key</p>",
                    "content_text": "I'm blocked waiting on the API key",
                    "sender": "Developer",
                    "sender_email": "dev@test.com",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "chat_type": "oneOnOne",
                    "mentions": [],
                    "reactions": [],
                    "importance": "high",
                    "has_attachments": False,
                    "reply_to_id": None,
                }
            ],
            "channels": [],
            "errors": [],
        }

        export_path = tmp_path / "teams_chat_blocker.json"
        export_path.write_text(json.dumps(export))

        extraction = extractor.extract(export_path)

        assert len(extraction.signals) == 1
        signal = extraction.signals[0]
        assert signal.signal_type == "blocker"
        assert signal.confidence >= 0.9  # high importance

    def test_extract_decisions(self, extractor, tmp_path):
        """Messages with decisions get decision type."""
        export = {
            "exported_at": datetime.now(UTC).isoformat(),
            "time_window_days": 7,
            "messages": [
                {
                    "id": "msg3",
                    "content": "<p>We decided to go with approach B</p>",
                    "content_text": "We decided to go with approach B",
                    "sender": "Lead",
                    "sender_email": "lead@test.com",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "team_name": "Architecture",
                    "channel_name": "Decisions",
                    "chat_type": "channel",
                    "mentions": [],
                    "reactions": ["like", "like", "heart"],
                    "importance": "normal",
                    "has_attachments": False,
                    "reply_to_id": None,
                }
            ],
            "channels": [],
            "errors": [],
        }

        export_path = tmp_path / "teams_chat_decision.json"
        export_path.write_text(json.dumps(export))

        extraction = extractor.extract(export_path)

        assert len(extraction.signals) == 1
        signal = extraction.signals[0]
        assert signal.signal_type == "decision"
        assert "Architecture" in signal.metadata.get("team_name", "")


class TestTeamsChatFreshness:
    """Test freshness tracking for Teams Chat channel."""

    @pytest.fixture
    def extractor(self, ms_graph_creds):
        from axiom.extensions.builtins.signals.extractors.teams_chat import (
            TeamsChatExtractor,
        )
        return TeamsChatExtractor(
            client_id=ms_graph_creds["client_id"],
            client_secret=ms_graph_creds["client_secret"],
            tenant_id=ms_graph_creds["tenant_id"],
        )

    def test_freshness_tracking(self, extractor, freshness_tracker):
        """Verify freshness tracking mechanism."""
        channel = "teams_chat"

        # Initially not fresh
        assert not freshness_tracker.is_fresh(channel)

        # After marking synced, should be fresh
        freshness_tracker.mark_synced(channel)
        assert freshness_tracker.is_fresh(channel, max_age_hours=24)

    def test_stale_after_timeout(self, extractor, freshness_tracker, monkeypatch):
        """Channel becomes stale after max_age_hours."""
        channel = "teams_chat"

        # Mark synced
        freshness_tracker.mark_synced(channel)
        assert freshness_tracker.is_fresh(channel, max_age_hours=24)

        # Still fresh with short window
        assert freshness_tracker.is_fresh(channel, max_age_hours=1)

        # Would be stale after 0 hours (impossible window)
        assert not freshness_tracker.is_fresh(channel, max_age_hours=0)


class TestTeamsChannelMessages:
    """Test channel message handling specifically."""

    @pytest.fixture
    def extractor(self, ms_graph_creds):
        from axiom.extensions.builtins.signals.extractors.teams_chat import (
            TeamsChatExtractor,
        )
        return TeamsChatExtractor(
            client_id=ms_graph_creds["client_id"],
            client_secret=ms_graph_creds["client_secret"],
            tenant_id=ms_graph_creds["tenant_id"],
        )

    def test_message_location_in_detail(self, extractor, tmp_path):
        """Signal detail includes team/channel location."""
        export = {
            "exported_at": datetime.now(UTC).isoformat(),
            "time_window_days": 7,
            "messages": [
                {
                    "id": "loc1",
                    "content": "<p>Status update</p>",
                    "content_text": "Status update",
                    "sender": "PM",
                    "sender_email": "pm@test.com",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "team_name": "Example Team Core",
                    "channel_name": "Standup",
                    "chat_type": "channel",
                    "mentions": [],
                    "reactions": [],
                    "importance": "normal",
                    "has_attachments": False,
                    "reply_to_id": None,
                }
            ],
            "channels": [],
            "errors": [],
        }

        export_path = tmp_path / "teams_chat_location.json"
        export_path.write_text(json.dumps(export))

        extraction = extractor.extract(export_path)

        signal = extraction.signals[0]
        assert "[Example Team Core/Standup]" in signal.detail
        assert "PM:" in signal.detail
