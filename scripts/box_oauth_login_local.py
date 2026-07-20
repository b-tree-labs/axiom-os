#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Box OAuth login with a LOCAL callback — auto-captures the code.

Fixes the `app.box.com`-eats-the-code problem: runs a tiny localhost
server as the redirect target, so the browser hands the `?code=` straight
to us and we exchange it instantly (no manual copy, no 30s race). Acts as
the logging-in user; no enterprise admin (ITS) needed.

Prereq: add `http://localhost:8717` to the OAuth app's Redirect URIs.

Run on the laptop (where the browser is):
  python scripts/box_oauth_login_local.py --client-id ... --client-secret ...
It writes box-ccg.json locally; scp it to <ingest-host>:~/box-ccg.json.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import threading
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = "https://account.box.com/api/oauth2/authorize"
TOKEN_URL = "https://api.box.com/oauth2/token"
PORT = 8717
REDIRECT = f"http://localhost:{PORT}"

_result: dict = {}


def _exchange(code: str, cid: str, secret: str) -> dict:
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": code,
        "client_id": cid, "client_secret": secret, "redirect_uri": REDIRECT,
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data), timeout=30) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--client-secret", required=True)
    ap.add_argument("--out", default=os.path.expanduser("~/box-ccg.json"))
    args = ap.parse_args()

    cid, secret = args.client_id, args.client_secret

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def do_GET(self):
            qs = urllib.parse.urlparse(self.path).query
            code = urllib.parse.parse_qs(qs).get("code", [None])[0]
            msg = "No code in callback."
            if code:
                try:
                    body = _exchange(code, cid, secret)
                    rt = body.get("refresh_token")
                    if rt:
                        out = {"client_id": cid, "client_secret": secret,
                               "refresh_token": rt,
                               "token_store": os.path.expanduser(
                                   "~/.axi/credentials/box/oauth_refresh.json")}
                        with open(args.out, "w") as f:
                            json.dump(out, f)
                        os.chmod(args.out, 0o600)
                        _result["ok"] = True
                        msg = "Box login captured. You can close this tab."
                    else:
                        msg = f"No refresh_token: {body}"
                except Exception as e:  # noqa: BLE001
                    msg = f"Exchange failed: {e}"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(msg.encode())
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    srv = http.server.HTTPServer(("localhost", PORT), Handler)
    url = f"{AUTH_URL}?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT})
    print(f"\nOpening browser to log in… if it doesn't open, visit:\n  {url}\n")
    webbrowser.open(url)
    print(f"Waiting for the Box redirect on {REDIRECT} …")
    srv.serve_forever()
    if _result.get("ok"):
        print(f"\n✓ Wrote {args.out}. Now: scp {args.out} <ingest-host>:~/box-ccg.json")
        return 0
    print("\nLogin did not complete.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
