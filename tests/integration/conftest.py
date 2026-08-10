# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures and markers for integration tests.

Integration tests hit real external services (GitLab, OneDrive, etc.)
and require credentials via environment variables.

Usage:
    # Run only integration tests:
    pytest tests/integration/ -v -m integration

    # Run all tests EXCEPT integration:
    pytest tests/ -m "not integration"

    # Run a specific channel:
    pytest tests/integration/ -v -k gitlab
"""

import os
from datetime import UTC

import pytest


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "integration: marks tests that hit external services")
    config.addinivalue_line("markers", "gitlab: GitLab API integration")
    config.addinivalue_line("markers", "onedrive: OneDrive / MS Graph integration")
    config.addinivalue_line("markers", "teams: Teams transcript integration")
    config.addinivalue_line("markers", "voice: Voice memo / sense serve integration")
    config.addinivalue_line("markers", "inbox: Inbox note integration")


# ---------------------------------------------------------------------------
# Credential-check fixtures — skip tests when credentials are missing
# ---------------------------------------------------------------------------

@pytest.fixture
def gitlab_token():
    """GITLAB_TOKEN for the private GitLab instance."""
    token = os.environ.get("GITLAB_TOKEN")
    if not token:
        pytest.skip("GITLAB_TOKEN not set")
    return token


@pytest.fixture
def ms_graph_creds():
    """MS Graph credentials for OneDrive / Teams."""
    client_id = os.environ.get("MS_GRAPH_CLIENT_ID")
    client_secret = os.environ.get("MS_GRAPH_CLIENT_SECRET")
    tenant_id = os.environ.get("MS_GRAPH_TENANT_ID")
    if not client_id or not client_secret:
        pytest.skip("MS_GRAPH_CLIENT_ID / MS_GRAPH_CLIENT_SECRET not set")
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "tenant_id": tenant_id or "common",
    }


@pytest.fixture
def github_token():
    """GITHUB_TOKEN for GitHub API access."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        pytest.skip("GITHUB_TOKEN not set")
    return token


# ---------------------------------------------------------------------------
# Freshness tracking fixture — compare against last known update
# ---------------------------------------------------------------------------

@pytest.fixture
def freshness_tracker(tmp_path):
    """Track last sync time for channels."""
    import json
    from datetime import datetime

    state_file = tmp_path / "channel_freshness.json"

    class FreshnessTracker:
        def __init__(self):
            self.state = {}
            if state_file.exists():
                self.state = json.loads(state_file.read_text())

        def get_last_sync(self, channel: str) -> datetime | None:
            """Get the last sync time for a channel."""
            if ts := self.state.get(channel):
                return datetime.fromisoformat(ts)
            return None

        def mark_synced(self, channel: str) -> None:
            """Mark a channel as just synced."""
            self.state[channel] = datetime.now(UTC).isoformat()
            state_file.write_text(json.dumps(self.state, indent=2))

        def is_fresh(self, channel: str, max_age_hours: int = 24) -> bool:
            """Check if channel was synced within max_age_hours."""
            from datetime import timedelta
            last = self.get_last_sync(channel)
            if not last:
                return False
            age = datetime.now(UTC) - last
            return age < timedelta(hours=max_age_hours)

    return FreshnessTracker()
