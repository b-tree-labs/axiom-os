# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Box CCG auth — server auth with no keypair."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from axiom.extensions.builtins.data_platform.sources.box.ccg_auth import (
    BoxCcgAuth,
    BoxCcgConfig,
)

_CCG = {"client_id": "cid", "client_secret": "sec", "enterprise_id": "134853"}
_JWT = {"boxAppSettings": {"clientID": "x"}, "enterpriseID": "y"}


def test_is_ccg_blob_discriminates_from_jwt():
    assert BoxCcgConfig.is_ccg_blob(_CCG)
    assert not BoxCcgConfig.is_ccg_blob(_JWT)


def test_config_parses_and_stringifies_enterprise_id():
    cfg = BoxCcgConfig.from_dict({**_CCG, "enterprise_id": 134853})
    assert cfg.enterprise_id == "134853"


def test_mint_uses_client_credentials_grant_and_caches():
    auth = BoxCcgAuth(BoxCcgConfig.from_dict(_CCG))
    resp = MagicMock(ok=True)
    resp.json.return_value = {"access_token": "tok-1", "expires_in": 3600}
    with patch(
        "axiom.extensions.builtins.data_platform.sources.box.ccg_auth.requests.post",
        return_value=resp,
    ) as post:
        assert auth.authorization_header() == "Bearer tok-1"
        assert auth.get_access_token() == "tok-1"  # cached, no 2nd mint
    assert post.call_count == 1
    sent = post.call_args.kwargs["data"]
    assert sent["grant_type"] == "client_credentials"
    assert sent["box_subject_type"] == "enterprise"
    assert sent["box_subject_id"] == "134853"
