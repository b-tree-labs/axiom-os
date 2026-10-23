# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.box_login``: make one node's Box login through the owner's own OAuth app.

A node that publishes to Box needs a login that only it holds: Box renews a login
on every use and cancels the old one, so one login cannot be shared by two
machines. The person who runs the Box side (not the partner) runs this once per
node, signing in as the Box account made for that node. It opens Box's own login
page, catches the answer on this computer's loopback address, trades it for a
login, and writes that login, in the one-line form ``rclone`` reads, to a file
only the caller can read. The login is never printed.

The app's client id and secret are read from the vault (a JSON entry with
``client_id`` and ``client_secret``), never typed on a command line. The redirect
address must be one the app has registered; this uses ``http://localhost:<port>``.
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

AUTHORIZE_URL = "https://account.box.com/api/oauth2/authorize"
TOKEN_URL = "https://api.box.com/oauth2/token"
DEFAULT_PORT = 8717
BLOCK_PREFIX = '{"access_token"'

#: The two seams tests replace: opening the browser, and the call to Box's token endpoint.
_open = webbrowser.open


def _post(url: str, data: dict[str, str]) -> dict[str, Any]:  # pragma: no cover - network glue
    import requests

    r = requests.post(url, data=data, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Box refused the code (HTTP {r.status_code}); start again")
    return r.json()


_post_token = _post


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    q = urllib.parse.urlencode(
        {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": state}
    )
    return f"{AUTHORIZE_URL}?{q}"


def login_block(token: dict[str, Any], *, now: datetime | None = None) -> str:
    """The one-line login rclone reads: access token, type, refresh token, expiry."""
    now = now or datetime.now(UTC)
    expiry = now + timedelta(seconds=int(token.get("expires_in") or 3600))
    for need in ("access_token", "refresh_token"):
        if not token.get(need):
            raise ValueError(f"Box's answer has no {need}")
    return json.dumps(
        {
            "access_token": token["access_token"],
            "token_type": "bearer",
            "refresh_token": token["refresh_token"],
            "expiry": expiry.isoformat().replace("+00:00", "Z"),
        },
        separators=(",", ":"),
    )


class _Catcher(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] = {}
    done = threading.Event()

    def do_GET(self) -> None:  # noqa: N802
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        type(self).result = {k: v[0] for k, v in q.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"You can close this window. The login was made; return to the terminal.")
        type(self).done.set()

    def log_message(self, *_a: object) -> None:  # silence: the query string carries the code
        return


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    out = str(params.get("out") or "").strip()
    app = str(params.get("app") or "").strip()
    if not out or not app:
        return SkillResult(ok=False, errors=["give --app (the vault entry holding the Box app) and --out (the file to write)"])
    port = int(params.get("port") or DEFAULT_PORT)
    timeout_s = float(params.get("timeout_s") or 300)
    target = Path(out).expanduser()
    if target.exists():
        return SkillResult(ok=False, errors=[f"{target} already exists; choose a new file so a login is never overwritten"])

    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore

    vault = ForeignCredentialStore(ctx.state_dir)
    try:
        with vault.get(app) as secret:
            creds = json.loads(secret.as_str())
        client_id, client_secret = str(creds["client_id"]), str(creds["client_secret"])
    except (KeyError, ValueError) as exc:
        return SkillResult(ok=False, errors=[f"the vault entry {app!r} is not a Box app ({type(exc).__name__}): "
                                             "it needs JSON with client_id and client_secret"])

    redirect = f"http://localhost:{port}"
    state = secrets.token_urlsafe(24)
    catcher = type("Catcher", (_Catcher,), {"result": {}, "done": threading.Event()})
    try:
        server = http.server.HTTPServer(("127.0.0.1", port), catcher)
    except OSError as exc:
        return SkillResult(ok=False, errors=[f"port {port} is not free ({exc.strerror}); it is the redirect the app registered"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = authorize_url(client_id, redirect, state)
        opened = False
        if not params.get("no_browser"):
            try:
                opened = bool(_open(url))
            except Exception:  # noqa: BLE001
                opened = False
        actions = [] if opened else [f"open this address in a browser on this computer: {url}"]
        deadline = time.monotonic() + timeout_s
        while not catcher.done.wait(0.2):
            if time.monotonic() > deadline:
                return SkillResult(ok=False, errors=["no answer from Box in time; start again"], actions_taken=actions)
    finally:
        server.shutdown()
        server.server_close()

    got = catcher.result
    if got.get("state") != state:
        return SkillResult(ok=False, errors=["the answer did not carry this run's state; it was ignored. Start again"])
    if "error" in got or not got.get("code"):
        return SkillResult(ok=False, errors=[f"Box did not grant access ({got.get('error', 'no code')}); start again"])
    try:
        token = _post_token(
            TOKEN_URL,
            {"grant_type": "authorization_code", "code": got["code"], "client_id": client_id,
             "client_secret": client_secret, "redirect_uri": redirect},
        )
        block = login_block(token)
    except (RuntimeError, ValueError) as exc:
        return SkillResult(ok=False, errors=[str(exc)], actions_taken=actions)

    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(block + "\n")
    actions.append(f"wrote the login to {target} (readable only by you); it starts with {BLOCK_PREFIX}")
    actions.append("next: send that one line to the partner by a secure channel, then delete the file")
    return SkillResult(ok=True, value={"out": str(target), "starts_with": BLOCK_PREFIX}, actions_taken=actions)
