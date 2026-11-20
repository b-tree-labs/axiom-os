# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The Box login a node uses is made once, through the owner's app, and never printed.

A real Box is not involved: the browser step and Box's token endpoint are the two
seams, replaced here. Everything between them (the loopback catch, the state check,
the file, its permissions, what is and is not said aloud) is the real code.
"""

from __future__ import annotations

import json
import socket
import stat
import threading
import urllib.parse
import urllib.request
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.data_platform.skills import box_login
from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore

APP = {"client_id": "cid-123", "client_secret": "csecret-456"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def world(tmp_path, monkeypatch):
    ForeignCredentialStore(tmp_path / "state").set("box-app", json.dumps(APP).encode())
    seen: dict = {}

    def fake_browser(url: str) -> bool:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        seen["url"] = url
        redirect = q["redirect_uri"][0]
        state = seen.get("force_state", q["state"][0])
        code = seen.get("code", "the-code")
        threading.Thread(
            target=lambda: urllib.request.urlopen(f"{redirect}/?code={code}&state={state}", timeout=10).read(),
            daemon=True,
        ).start()
        return True

    def fake_token(url: str, data: dict) -> dict:
        seen["token_request"] = (url, data)
        return {"access_token": "AT-secret", "refresh_token": "RT-secret", "expires_in": 3600}

    monkeypatch.setattr(box_login, "_open", fake_browser)
    monkeypatch.setattr(box_login, "_post_token", fake_token)
    ctx = SimpleNamespace(state_dir=tmp_path / "state")
    return SimpleNamespace(ctx=ctx, seen=seen, out=tmp_path / "node.txt", port=_free_port())


def _run(w, **extra):
    return box_login.run({"app": "box-app", "out": str(w.out), "port": w.port, "timeout_s": 20, **extra}, w.ctx)


def test_a_login_is_written_for_rclone_and_only_the_caller_can_read_it(world):
    r = _run(world)
    assert r.ok, r.errors
    text = world.out.read_text()
    assert text.startswith(box_login.BLOCK_PREFIX) and text.count("\n") == 1
    block = json.loads(text)
    assert block["refresh_token"] == "RT-secret" and block["token_type"] == "bearer" and block["expiry"].endswith("Z")
    assert stat.S_IMODE(world.out.stat().st_mode) == 0o600


def test_the_app_credentials_and_code_go_only_to_boxs_token_endpoint(world):
    _run(world)
    url, data = world.seen["token_request"]
    assert url == box_login.TOKEN_URL
    assert data["client_id"] == "cid-123" and data["client_secret"] == "csecret-456" and data["code"] == "the-code"
    assert data["redirect_uri"] == f"http://localhost:{world.port}"
    assert "client_secret" not in world.seen["url"], "the secret is never in the address the browser opens"


def test_nothing_secret_is_said_aloud(world):
    r = _run(world)
    said = json.dumps({"value": r.value, "actions": r.actions_taken, "errors": r.errors})
    for secret in ("AT-secret", "RT-secret", "csecret-456"):
        assert secret not in said


def test_an_answer_that_does_not_carry_our_state_is_ignored(world):
    world.seen["force_state"] = "someone-elses"
    r = _run(world)
    assert not r.ok and "state" in r.errors[0]
    assert not world.out.exists() and "token_request" not in world.seen


def test_a_login_is_never_overwritten(world):
    world.out.write_text("existing")
    r = _run(world)
    assert not r.ok and "already exists" in r.errors[0]
    assert world.out.read_text() == "existing"


def test_an_entry_that_is_not_a_box_app_is_refused(world):
    ForeignCredentialStore(world.ctx.state_dir).set("box-app", b'{"nope": 1}')
    r = _run(world)
    assert not r.ok and "not a Box app" in r.errors[0]


def test_a_busy_redirect_port_is_said_plainly(world):
    with socket.socket() as s:
        s.bind(("127.0.0.1", world.port))
        s.listen(1)
        r = _run(world)
    assert not r.ok and "not free" in r.errors[0]
