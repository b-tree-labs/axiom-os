# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Box OAuth refresh-token auth — shape detection + rotation persistence."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from axiom.extensions.builtins.data_platform.sources.box.oauth_auth import (
    BoxOAuthAuth, BoxOAuthConfig,
)

_OAUTH = {"client_id": "c", "client_secret": "s", "refresh_token": "r1"}


def test_is_oauth_blob_discriminates():
    assert BoxOAuthConfig.is_oauth_blob(_OAUTH)
    assert not BoxOAuthConfig.is_oauth_blob({"boxAppSettings": {}})        # JWT
    assert not BoxOAuthConfig.is_oauth_blob(
        {"client_id": "c", "client_secret": "s", "enterprise_id": "e"})    # CCG


def test_seed_and_rotate(tmp_path):
    store = tmp_path / "rt.json"
    cfg = BoxOAuthConfig.from_dict({**_OAUTH, "token_store": str(store)})
    assert json.loads(store.read_text())["refresh_token"] == "r1"   # seeded
    auth = BoxOAuthAuth(cfg)
    resp = MagicMock(ok=True)
    resp.json.return_value = {"access_token": "AT1", "refresh_token": "r2", "expires_in": 3600}
    with patch(
        "axiom.extensions.builtins.data_platform.sources.box.oauth_auth.requests.post",
        return_value=resp,
    ) as post:
        assert auth.authorization_header() == "Bearer AT1"
        assert auth.get_access_token() == "AT1"     # cached, no 2nd call
    assert post.call_count == 1
    assert post.call_args.kwargs["data"]["grant_type"] == "refresh_token"
    assert json.loads(store.read_text())["refresh_token"] == "r2"   # ROTATED + persisted
