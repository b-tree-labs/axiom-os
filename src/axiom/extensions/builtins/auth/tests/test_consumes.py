# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""AEOS-2 / AEOS-ID-2: resolve a consumes-credential declaration to a
token_source, enforcing the posture floor first."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.auth.consumes import PostureError, resolve_credential
from axiom.extensions.builtins.auth.token_store import (
    InMemoryTokenStore,
    store_refresh_token,
)
from axiom.infra.principal import PrincipalContext

DECL = {
    "kind": "credential",
    "idp": "entra",
    "tenant": "example-tenant",
    "scopes": ["https://graph.microsoft.com/Calendars.ReadWrite", "offline_access"],
    "min_posture": "sso",
}


class _FakeHttp:
    def __init__(self, responses):
        self._r = list(responses)

    def post(self, url, data):
        return self._r.pop(0)


def test_under_assured_principal_is_blocked_by_posture_floor():
    open_p = PrincipalContext("@ben:local", "open")          # below the 'sso' floor
    with pytest.raises(PostureError, match="step up"):
        resolve_credential(DECL, principal=open_p, http=None, client_id="cid",
                           store=InMemoryTokenStore())


def test_assured_principal_resolves_a_working_token_source():
    sso_p = PrincipalContext("@ben:example", "sso", assured=True)
    store = InMemoryTokenStore()
    store_refresh_token("entra", "@ben:example", DECL["scopes"], "rt-1", store=store)
    http = _FakeHttp([{"access_token": "at-1", "expires_in": 3600}])

    ts = resolve_credential(DECL, principal=sso_p, http=http, client_id="cid", store=store)
    assert ts() == "at-1"                                    # the extension just gets fresh tokens


def test_open_floor_credential_works_on_an_open_node():
    open_p = PrincipalContext("@ben:local", "open")
    decl = {**DECL, "idp": "google", "min_posture": "open"}
    decl.pop("tenant")
    store = InMemoryTokenStore()
    store_refresh_token("google", "@ben:local", decl["scopes"], "rt-2", store=store)
    http = _FakeHttp([{"access_token": "at-2", "expires_in": 3600}])
    ts = resolve_credential(decl, principal=open_p, http=http, client_id="cid", store=store)
    assert ts() == "at-2"
