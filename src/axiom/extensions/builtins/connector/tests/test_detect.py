# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Connector detection primitive — uniform across all connector types (ADR-068)."""

from __future__ import annotations

from axiom.extensions.builtins.connector.detect import (
    ConnectorState,
    DetectResult,
    default_detect,
    detect_connector,
)


def _d(**kw):
    base = dict(
        vendor="slack",
        name="bens",
        secrets_present=False,
        secrets_complete=False,
        last_reconnect_required=None,
        last_ok=None,
        health=None,
    )
    base.update(kw)
    return default_detect(**base)


# --- the four states ------------------------------------------------------- #
def test_absent_when_nothing_configured():
    r = _d()
    assert r.state is ConnectorState.ABSENT
    assert r.actionable
    assert "not configured" in r.summary


def test_partial_when_secrets_incomplete():
    r = _d(secrets_present=True, secrets_complete=False)
    assert r.state is ConnectorState.PARTIAL
    assert "incomplete" in r.summary


def test_configured_when_health_passes():
    r = _d(secrets_present=True, secrets_complete=True, health=lambda: True)
    assert r.state is ConnectorState.CONFIGURED
    assert not r.actionable
    assert r.next_action is None


def test_broken_when_health_fails():
    r = _d(secrets_present=True, secrets_complete=True, health=lambda: False)
    assert r.state is ConnectorState.BROKEN
    assert "reconnect" in (r.next_action or "").lower()


def test_broken_when_health_raises():
    def boom():
        raise RuntimeError("network")

    r = _d(secrets_present=True, secrets_complete=True, health=boom)
    assert r.state is ConnectorState.BROKEN


# --- live health is authoritative over stale status ------------------------ #
def test_health_overrides_stale_ok_status():
    # status said ok, but live check fails now → BROKEN
    r = _d(
        secrets_present=True,
        secrets_complete=True,
        last_ok=True,
        health=lambda: False,
    )
    assert r.state is ConnectorState.BROKEN


# --- status-store fallback when no live check ------------------------------ #
def test_configured_from_last_ok_without_health():
    r = _d(secrets_present=True, secrets_complete=True, last_ok=True)
    assert r.state is ConnectorState.CONFIGURED


def test_broken_from_reconnect_required_without_health():
    r = _d(
        secrets_present=True,
        secrets_complete=True,
        last_reconnect_required=True,
        last_ok=False,
    )
    assert r.state is ConnectorState.BROKEN


def test_partial_when_creds_present_but_never_verified():
    r = _d(secrets_present=True, secrets_complete=True)
    assert r.state is ConnectorState.PARTIAL
    assert "verif" in (r.next_action or "").lower()


# --- vendor override path -------------------------------------------------- #
class _RichHandler:
    vendor = "slack"

    def detect(self, **probes):
        # e.g. found a Slack CLI session — richer than the generic probe
        return DetectResult(
            ConnectorState.CONFIGURED,
            summary="Reused Slack CLI session for SoilMetrix.",
            details={"source": "slack-cli"},
        )


class _PlainHandler:
    vendor = "twilio_sms"


def test_detect_connector_prefers_vendor_override():
    r = detect_connector(
        _RichHandler(),
        name="bens",
        secrets_present=False,
        secrets_complete=False,
        last_reconnect_required=None,
        last_ok=None,
    )
    assert r.state is ConnectorState.CONFIGURED
    assert r.details["source"] == "slack-cli"


def test_detect_connector_falls_back_to_default():
    r = detect_connector(
        _PlainHandler(),
        name="bens",
        secrets_present=False,
        secrets_complete=False,
        last_reconnect_required=None,
        last_ok=None,
    )
    assert r.state is ConnectorState.ABSENT
    assert r.summary.startswith("twilio_sms")
