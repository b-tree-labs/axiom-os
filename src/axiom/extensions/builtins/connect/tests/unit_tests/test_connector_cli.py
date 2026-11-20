# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""CLI-layer polish for the connector verbs: case-insensitive reference
resolution + did-you-mean suggestions (ADR-074)."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.connect.connectors import (
    slack_connector_descriptor,
    teams_connector_descriptor,
)
from axiom.infra.connector_fabric import (
    InProcessConnectorFabric,
)

cli = pytest.importorskip("axiom.extensions.builtins.connect.connector_cli")


@pytest.fixture
def fabric():
    f = InProcessConnectorFabric()
    f.register(slack_connector_descriptor())
    f.register(teams_connector_descriptor())
    return f


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("slack", "ai.axiom.connector.slack"),
        ("Slack", "ai.axiom.connector.slack"),
        ("SLACK", "ai.axiom.connector.slack"),
        ("ai.axiom.connector.slack", "ai.axiom.connector.slack"),
        ("teams", "ai.axiom.connector.teams"),
        ("Microsoft Teams", "ai.axiom.connector.teams"),
        ("  slack  ", "ai.axiom.connector.slack"),
    ],
)
def test_resolve_name_is_case_and_form_insensitive(fabric, ref, expected):
    assert cli._resolve_name(fabric, ref) == expected


def test_resolve_unknown_returns_none(fabric):
    assert cli._resolve_name(fabric, "discord") is None


def test_suggest_offers_close_matches(fabric):
    # typo → the intended connector is suggested
    assert "ai.axiom.connector.slack" in cli._suggest(fabric, "slak")
    assert "ai.axiom.connector.teams" in cli._suggest(fabric, "team")


def test_suggest_empty_for_nonsense(fabric):
    assert cli._suggest(fabric, "zzzzzzz") == []


def test_shorten_url_passes_through_short_urls():
    # short URLs are returned unchanged (no network); long ones are shortened
    # best-effort and always return an http(s) URL even offline.
    assert cli.shorten_url("https://x.co/abc") == "https://x.co/abc"
