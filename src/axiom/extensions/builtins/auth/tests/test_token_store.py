# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""AUTH-4: vault-keyed refresh-token custody + the (provider,user,scopes) token
source factory."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.auth import providers
from axiom.extensions.builtins.auth.token_store import (
    InMemoryTokenStore,
    load_refresh_token,
    store_refresh_token,
    token_key,
    token_source,
)


class _FakeHttp:
    def __init__(self, responses):
        self._r = list(responses)
        self.posts = []

    def post(self, url, data):
        self.posts.append((url, data))
        return self._r.pop(0)


def test_token_key_is_scope_order_independent():
    a = token_key("entra", "ben@x", ["openid", "email"])
    b = token_key("entra", "ben@x", ["email", "openid"])
    assert a == b
    assert token_key("entra", "ben@x", ["openid"]) != a   # different scope-set, different key


def test_store_and_load_roundtrip():
    s = InMemoryTokenStore()
    store_refresh_token("entra", "ben@x", ["openid"], "rt-123", store=s)
    assert load_refresh_token("entra", "ben@x", ["openid"], store=s) == "rt-123"
    assert load_refresh_token("entra", "other@x", ["openid"], store=s) is None


def test_token_source_refreshes_from_stored_token():
    s = InMemoryTokenStore()
    store_refresh_token("google", "ben@x", ["openid"], "rt-1", store=s)
    http = _FakeHttp([{"access_token": "at-1", "expires_in": 3600}])
    ts = token_source(provider=providers.google(), user="ben@x", scopes=["openid"],
                      http=http, client_id="cid", store=s)
    assert ts() == "at-1"
    assert http.posts[0][1]["refresh_token"] == "rt-1"     # used the stored token


def test_token_source_requires_prior_login():
    with pytest.raises(LookupError, match="axi auth login"):
        token_source(provider=providers.google(), user="nobody@x", scopes=["openid"],
                     http=None, client_id="cid", store=InMemoryTokenStore())
