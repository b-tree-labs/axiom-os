# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The auth<->calendar bridge: a calendar connection resolves a posture-gated
delegated token_source and builds the provider authed as the user."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.auth.consumes import PostureError
from axiom.extensions.builtins.auth.token_store import InMemoryTokenStore, store_refresh_token
from axiom.extensions.builtins.schedule.calendar.connect import connect_calendar
from axiom.infra.principal import PrincipalContext


class _FakeHttp:
    def __init__(self, responses):
        self._r = list(responses)

    def post(self, url, data):
        return self._r.pop(0)


SSO = PrincipalContext("@ben:example", "sso", assured=True, idp="entra")
OPEN = PrincipalContext("@ben:local", "open")


def test_m365_connection_acts_as_the_user_via_delegated_token():
    store = InMemoryTokenStore()
    scopes = ["https://graph.microsoft.com/Calendars.ReadWrite", "offline_access"]
    store_refresh_token("entra", "@ben:example", scopes, "rt-1", store=store)
    http = _FakeHttp([{"access_token": "graph-at", "expires_in": 3600}])

    provider = connect_calendar(
        "m365", principal=SSO, http=http, client_id="cid", tenant="example-tenant",
        user_id="@ben:example", store=store, calendar_id="cal-1",
    )
    # The provider's Graph client carries a delegated bearer from the user's token.
    assert provider.vendor == "m365"
    assert provider._c()._headers()["Authorization"] == "Bearer graph-at"


def test_connection_blocked_below_posture_floor():
    with pytest.raises(PostureError):
        connect_calendar("google", principal=OPEN, http=None, client_id="cid",
                         min_posture="sso", store=InMemoryTokenStore())


def test_connection_works_on_open_node_when_floor_is_open():
    store = InMemoryTokenStore()
    scopes = ["https://www.googleapis.com/auth/calendar", "openid", "email"]
    store_refresh_token("google", "@ben:local", scopes, "rt-2", store=store)
    http = _FakeHttp([{"access_token": "g-at", "expires_in": 3600}])
    provider = connect_calendar("google", principal=OPEN, http=http, client_id="cid",
                                user_id="@ben:local", store=store)
    assert provider.vendor == "google"


def test_unknown_vendor_without_explicit_idp_is_a_clear_error_not_entra():
    """Regression (connector tech debt): an unknown calendar vendor used to
    silently default its IdP to Entra. It must now demand an explicit idp=."""
    with pytest.raises(ValueError, match="no configured IdP for calendar vendor 'zoho'"):
        connect_calendar("zoho", principal=SSO, http=_FakeHttp([]),
                         client_id="cid", user_id="@ben:example")


# --- Vendor mapping is deployment config, not code (ADR-110 §Decision-6) ------

def test_vendor_config_defaults_when_no_override(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_CONFIG_DIR", str(tmp_path))  # empty: no override file
    from axiom.extensions.builtins.schedule.calendar.connect import _vendor_config
    cfg = _vendor_config()
    assert cfg["m365"]["idp"] == "entra"
    assert cfg["google"]["idp"] == "google"
    assert "offline_access" in cfg["m365"]["scopes"]


def test_deployment_override_remaps_and_adds_vendors_without_a_code_patch(tmp_path, monkeypatch):
    """The whole point: an operator edits a config file in a live deployment and
    the vendor->IdP mapping changes — no Python edit, no wheel ship."""
    monkeypatch.setenv("AXIOM_CONFIG_DIR", str(tmp_path))
    (tmp_path / "calendar-vendors.toml").write_text(
        '[vendors.m365]\nidp = "okta"\nscopes = ["cal.readwrite"]\n'
        '[vendors.zoho]\nidp = "google"\nscopes = ["openid"]\n'
    )
    from axiom.extensions.builtins.schedule.calendar.connect import _vendor_config
    cfg = _vendor_config()
    assert cfg["m365"]["idp"] == "okta"       # remapped by config alone
    assert cfg["zoho"]["idp"] == "google"     # brand-new vendor by config alone
    assert cfg["google"]["idp"] == "google"   # untouched default survives the merge


def test_no_vendor_mapping_literals_remain_in_connect_module():
    """Regression guard for Ben's directive: the module must hold no hardcoded
    vendor->idp/scopes dict — that data lives in vendor_defaults.toml / config."""
    from axiom.extensions.builtins.schedule.calendar import connect
    assert not hasattr(connect, "_VENDOR_IDP")
    assert not hasattr(connect, "_VENDOR_SCOPES")
