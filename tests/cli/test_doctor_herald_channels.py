# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the HERALD declared-vs-registered channel check in `axi doctor`.

The failure this guards: channel rehydration is env-only and fail-closed, so
a webhook var that evaporates with a shell session silently unregisters the
channel and every send falls back to the inbox — a validated alert path goes
dark and nothing notices until a human stares at an empty Teams channel.
"""

from __future__ import annotations

import pytest


def _clear(monkeypatch):
    # hermetic: ignore any real herald.toml on the machine
    from axiom.extensions.builtins.notifications import channel_config

    monkeypatch.setattr(channel_config, "_load_installed", dict)
    for var in (
        "AXIOM_HERALD_EXPECTED_CHANNELS",
        "AXIOM_HERALD_TEAMS_WEBHOOK_URL",
        "AXIOM_HERALD_SLACK_WEBHOOK_URL",
        "AXIOM_HERALD_MATTERMOST_WEBHOOK_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_skipped_when_nothing_declared(monkeypatch):
    from axiom.cli.doctor import CheckStatus, check_herald_channels

    _clear(monkeypatch)
    result = check_herald_channels()
    assert result.status == CheckStatus.SKIPPED
    assert "AXIOM_HERALD_EXPECTED_CHANNELS" in result.summary or "setup" in result.summary


def test_ok_when_declared_channels_registered(monkeypatch):
    from axiom.cli.doctor import CheckStatus, check_herald_channels

    _clear(monkeypatch)
    monkeypatch.setenv("AXIOM_HERALD_EXPECTED_CHANNELS", "inbox")
    result = check_herald_channels()
    assert result.status == CheckStatus.OK
    assert "inbox" in result.summary


def test_error_when_declared_channel_not_registered(monkeypatch):
    from axiom.cli.doctor import CheckStatus, check_herald_channels

    _clear(monkeypatch)
    # teams declared but its webhook var unset -> fail-closed rehydration
    # silently drops it; doctor must turn that silence into an alarm.
    monkeypatch.setenv("AXIOM_HERALD_EXPECTED_CHANNELS", "inbox,teams")
    result = check_herald_channels()
    assert result.status == CheckStatus.ERROR
    assert "teams" in result.summary
    assert "AXIOM_HERALD_TEAMS_WEBHOOK_URL" in (result.fix_hint or "")
    assert (result.detail or {}).get("missing") == ["teams"]


def test_ok_when_declared_channel_rehydrates(monkeypatch):
    pytest.importorskip("httpx")
    from axiom.cli.doctor import CheckStatus, check_herald_channels

    _clear(monkeypatch)
    monkeypatch.setenv("AXIOM_HERALD_EXPECTED_CHANNELS", "inbox,teams")
    monkeypatch.setenv(
        "AXIOM_HERALD_TEAMS_WEBHOOK_URL",
        "https://example.logic.azure.com/workflows/x/triggers/manual/paths/invoke?sig=y",
    )
    result = check_herald_channels()
    assert result.status == CheckStatus.OK
    assert "teams" in result.summary
