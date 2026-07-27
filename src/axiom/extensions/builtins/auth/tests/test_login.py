# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""AUTH-6: device-code login orchestration (end to end vs a fake IdP) + the
`axi auth providers` CLI subprocess smoke."""

from __future__ import annotations

import base64
import json
import subprocess
import sys

from axiom.extensions.builtins.auth import providers
from axiom.extensions.builtins.auth.login import login_with_device_code
from axiom.extensions.builtins.auth.token_store import InMemoryTokenStore, load_refresh_token


class _FakeHttp:
    def __init__(self, responses):
        self._r = list(responses)

    def post(self, url, data):
        return self._r.pop(0)


def _id_token(claims):
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


def test_login_derives_user_from_id_token_and_stores_refresh():
    store = InMemoryTokenStore()
    http = _FakeHttp([
        {"device_code": "dc", "user_code": "ABCD", "verification_uri": "https://x/dev", "interval": 0},
        {"access_token": "at", "refresh_token": "rt",
         "id_token": _id_token({"preferred_username": "user@example.org"}), "expires_in": 3600},
    ])
    prompts = []
    result = login_with_device_code(
        idp=providers.entra("example-tenant"), client_id="cid",
        scopes=["openid", "offline_access"], http=http, store=store,
        prompt=prompts.append, sleep=lambda _s: None,
    )
    assert result["user"] == "user@example.org" and result["stored"] is True
    assert any("ABCD" in p for p in prompts)                       # user code shown
    assert load_refresh_token("entra", "user@example.org", ["openid", "offline_access"], store=store) == "rt"


def test_login_falls_back_to_explicit_user_without_id_token():
    store = InMemoryTokenStore()
    http = _FakeHttp([
        {"device_code": "dc", "user_code": "WXYZ", "verification_uri": "https://x/dev"},
        {"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
    ])
    result = login_with_device_code(
        idp=providers.google(), client_id="cid", scopes=["openid"], http=http,
        store=store, user="ben@x", sleep=lambda _s: None,
    )
    assert result["user"] == "ben@x" and result["stored"] is True


def test_cli_providers_subprocess_smoke():
    # Anchor-robust: point the subprocess at THIS worktree's src so an editable
    # install anchored on a sibling worktree can't shadow the new code.
    import os
    from pathlib import Path

    src_root = Path(__file__).resolve().parents[5]            # .../src
    env = {**os.environ, "PYTHONPATH": f"{src_root}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"}
    proc = subprocess.run(
        [sys.executable, "-m", "axiom.extensions.builtins.auth.cli", "providers"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "entra" in proc.stdout and "google" in proc.stdout
