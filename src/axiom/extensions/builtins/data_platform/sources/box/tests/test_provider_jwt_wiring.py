# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""BoxSourceProvider resolves JWT auth via SecretRef (not bare values)."""

from __future__ import annotations

import json

from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
    ConnectorConfig,
)
from axiom.extensions.builtins.data_platform.sources.box.jwt_auth import BoxJwtAuth
from axiom.extensions.builtins.data_platform.sources.box.provider import (
    BoxSourceProvider,
)

_FAKE_CONFIG = {
    "boxAppSettings": {
        "clientID": "cid",
        "clientSecret": "sec",
        "appAuth": {
            "publicKeyID": "kid",
            "privateKey": "-----BEGIN ENCRYPTED PRIVATE KEY-----\nx\n-----END ENCRYPTED PRIVATE KEY-----",
            "passphrase": "pp",
        },
    },
    "enterpriseID": "eid",
}


def test_jwt_secret_ref_resolves_via_env_provider(monkeypatch):
    """A jwt_secret_ref of env://VAR resolves through the SecretStore and
    yields a BoxJwtAuth — proving creds flow via SecretRef, not raw env."""
    monkeypatch.setenv("TEST_BOX_JWT", json.dumps(_FAKE_CONFIG))
    cfg = ConnectorConfig(
        name="dmsr", kind="box", bronze_root="/tmp/bronze",
        params={"folder_id": "123", "jwt_secret_ref": "env://TEST_BOX_JWT"},
    )
    auth = BoxSourceProvider()._resolve_jwt_auth(cfg)
    assert isinstance(auth, BoxJwtAuth)


def test_no_jwt_ref_returns_none():
    cfg = ConnectorConfig(name="dmsr", kind="box", bronze_root="/tmp/bronze", params={"folder_id": "123"})
    assert BoxSourceProvider()._resolve_jwt_auth(cfg) is None
