# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""AUTH-3 (RFC 8628): device-code flow — start + poll-until-approved, against a
fake IdP (no network, no sleeping)."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.auth import providers
from axiom.extensions.builtins.auth.device_flow import (
    DeviceFlowError,
    poll_for_token,
    start_device_flow,
)


class _FakeHttp:
    def __init__(self, responses):
        self._r = list(responses)
        self.posts = []

    def post(self, url, data):
        self.posts.append((url, data))
        return self._r.pop(0)


def test_start_device_flow_returns_user_code():
    idp = providers.entra("example-tenant")
    http = _FakeHttp([{"device_code": "dc-1", "user_code": "ABCD-EFGH",
                       "verification_uri": "https://microsoft.com/devicelogin", "interval": 5}])
    resp = start_device_flow(http, idp, client_id="cid", scopes=["openid"])
    assert resp["user_code"] == "ABCD-EFGH"
    assert http.posts[0][0] == idp.device_authorization_endpoint


def test_poll_waits_through_pending_then_succeeds():
    idp = providers.google()
    http = _FakeHttp([
        {"error": "authorization_pending"},
        {"error": "slow_down"},
        {"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
    ])
    waited = []
    tok = poll_for_token(http, idp, client_id="cid", device_code="dc-1",
                         interval=5, sleep=waited.append)
    assert tok["access_token"] == "at" and tok["refresh_token"] == "rt"
    assert len(http.posts) == 3                 # pending, slow_down, success
    assert waited and waited[-1] > 5            # slow_down backed off


def test_poll_raises_on_error():
    idp = providers.google()
    http = _FakeHttp([{"error": "access_denied"}])
    with pytest.raises(DeviceFlowError, match="access_denied"):
        poll_for_token(http, idp, client_id="cid", device_code="dc-1", sleep=lambda _s: None)
