# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Track E: the `axi calendar connect` wizard — sign in (device-code) then build
the authed provider and verify with a live round-trip."""

from __future__ import annotations

import base64
import json

from axiom.extensions.builtins.auth.token_store import InMemoryTokenStore, load_refresh_token
from axiom.extensions.builtins.schedule.calendar.connect import (
    _VENDOR_SCOPES,
    connect_and_verify,
)
from axiom.extensions.builtins.schedule.calendar.vendors.fake import FakeCalendarProvider


class _FakeHttp:
    def __init__(self, responses):
        self._r = list(responses)

    def post(self, url, data):
        return self._r.pop(0)


def _id_token(claims):
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


def test_connect_wizard_signs_in_then_round_trips():
    store = InMemoryTokenStore()
    http = _FakeHttp([
        {"device_code": "dc", "user_code": "ABCD", "verification_uri": "https://x/dev", "interval": 0},
        {"access_token": "at", "refresh_token": "rt",
         "id_token": _id_token({"preferred_username": "user@example.org"}), "expires_in": 3600},
    ])
    fake = FakeCalendarProvider()   # the round-trip runs against this (no network)

    res = connect_and_verify(
        "m365", idp_http=http, client_id="cid", tenant="example-tenant",
        store=store, provider=fake, sleep=lambda _s: None,
    )

    assert res["verified"] is True
    assert res["user"] == "user@example.org"          # derived from the id_token
    assert len(res["next_fires"]) == 3              # recurring round-trip bound + computed
    # the refresh token landed in the vault-keyed store
    assert load_refresh_token("entra", "user@example.org", _VENDOR_SCOPES["m365"], store=store) == "rt"
    assert fake._events == {}                       # round-trip event cleaned up
