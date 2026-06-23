# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``BoxJwtAuth`` — server-to-server JWT auth for the Box connector.

Ends the 60-min dev-token cliff. Reads a Box JWT app keypair config
(downloadable as JSON from the Box developer console after creating a
"Server Authentication (JWT)" app), mints fresh 60-min access tokens
on demand, caches them, refreshes 5 min before expiry. Daemon never
asks the operator for a token again.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from axiom.extensions.builtins.data_platform.sources.box.jwt_auth import (
    BoxJwtAuth,
    BoxJwtConfig,
)


_FAKE_CONFIG = {
    "boxAppSettings": {
        "clientID": "fake-client-id",
        "clientSecret": "fake-secret",
        "appAuth": {
            "publicKeyID": "fake-key-id",
            "privateKey": "-----BEGIN ENCRYPTED PRIVATE KEY-----\nfake\n-----END ENCRYPTED PRIVATE KEY-----",
            "passphrase": "fake-passphrase",
        },
    },
    "enterpriseID": "fake-enterprise-id",
}


def _write_config(tmp_path: Path) -> Path:
    p = tmp_path / "jwt-config.json"
    p.write_text(json.dumps(_FAKE_CONFIG))
    return p


# -- config parsing ----------------------------------------------------------


def test_box_jwt_config_loads_from_json_path(tmp_path):
    p = _write_config(tmp_path)
    cfg = BoxJwtConfig.from_json_path(p)
    assert cfg.client_id == "fake-client-id"
    assert cfg.enterprise_id == "fake-enterprise-id"
    assert cfg.passphrase == "fake-passphrase"


def test_box_jwt_config_loads_from_env_var(monkeypatch):
    monkeypatch.setenv("BOX_JWT_CONFIG", json.dumps(_FAKE_CONFIG))
    cfg = BoxJwtConfig.from_env("BOX_JWT_CONFIG")
    assert cfg.client_id == "fake-client-id"


def test_box_jwt_config_missing_env_raises(monkeypatch):
    monkeypatch.delenv("BOX_JWT_CONFIG", raising=False)
    with pytest.raises(RuntimeError, match="BOX_JWT_CONFIG"):
        BoxJwtConfig.from_env("BOX_JWT_CONFIG")


def test_box_jwt_config_invalid_json_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json {")
    with pytest.raises(ValueError):
        BoxJwtConfig.from_json_path(p)


# -- token minting + caching --------------------------------------------------


def test_get_access_token_calls_box_sdk_and_caches(tmp_path):
    cfg = BoxJwtConfig.from_json_path(_write_config(tmp_path))
    auth = BoxJwtAuth(cfg)

    fake_token = "fake-access-token-abc123"
    with patch.object(BoxJwtAuth, "_mint", return_value=(fake_token, time.time() + 3600)) as mint:
        t1 = auth.get_access_token()
        t2 = auth.get_access_token()

    assert t1 == fake_token
    assert t2 == fake_token
    # Cached — mint only called once
    assert mint.call_count == 1


def test_get_access_token_refreshes_when_near_expiry(tmp_path):
    cfg = BoxJwtConfig.from_json_path(_write_config(tmp_path))
    auth = BoxJwtAuth(cfg, refresh_window_s=300)

    now = time.time()
    # First call: token expires in 200s (< 300s refresh window) — should mint
    # Second call: should mint again because still in window
    with patch.object(BoxJwtAuth, "_mint",
                      side_effect=[("tok-1", now + 200), ("tok-2", now + 3600)]) as mint:
        t1 = auth.get_access_token()
        t2 = auth.get_access_token()

    assert t1 == "tok-1"
    assert t2 == "tok-2"
    assert mint.call_count == 2


def test_get_access_token_caches_until_near_expiry(tmp_path):
    cfg = BoxJwtConfig.from_json_path(_write_config(tmp_path))
    auth = BoxJwtAuth(cfg, refresh_window_s=300)

    now = time.time()
    # Token expires in 1 hour — well outside refresh window
    with patch.object(BoxJwtAuth, "_mint", return_value=("tok-1", now + 3600)) as mint:
        for _ in range(5):
            auth.get_access_token()

    assert mint.call_count == 1


# -- integration shape -------------------------------------------------------


def test_box_jwt_auth_exposes_authorization_header_method(tmp_path):
    cfg = BoxJwtConfig.from_json_path(_write_config(tmp_path))
    auth = BoxJwtAuth(cfg)

    with patch.object(BoxJwtAuth, "_mint", return_value=("tok-abc", time.time() + 3600)):
        hdr = auth.authorization_header()
        assert hdr == "Bearer tok-abc"
