#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""One-time Box OAuth 2.0 login → durable refresh token (no enterprise admin).

Removes the enterprise-admin (ITS) dependency: a standard OAuth app +
a single browser login yields a refresh token good for ~60 days of
auto-refreshing access tokens, acting AS the logging-in user.

Flow:
  1. Run this; it prints an authorize URL.
  2. Open it in a browser, log in to Box, approve.
  3. Box redirects to the app's redirect URI with `?code=...` in the URL bar.
  4. Paste that code here; it exchanges for tokens and writes the connector
     config (client_id/secret/refresh_token) + seeds the rotating store.

Run on the host that will ingest (the ingest server):
  python scripts/box_oauth_login.py --client-id ... --client-secret ... \
    --out ~/box-ccg.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

AUTH_URL = "https://account.box.com/api/oauth2/authorize"
TOKEN_URL = "https://api.box.com/oauth2/token"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-id", default=os.environ.get("BOX_CLIENT_ID", ""))
    ap.add_argument("--client-secret", default=os.environ.get("BOX_CLIENT_SECRET", ""))
    ap.add_argument("--redirect-uri", default="https://app.box.com",
                    help="must match the OAuth app's configured redirect URI")
    ap.add_argument("--out", default=os.path.expanduser("~/box-ccg.json"))
    ap.add_argument("--token-store",
                    default=os.path.expanduser("~/.axi/credentials/box/oauth_refresh.json"))
    args = ap.parse_args()

    if not (args.client_id and args.client_secret):
        print("need --client-id and --client-secret (from the OAuth app's "
              "Configuration → App Details)", file=sys.stderr)
        return 2

    q = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": args.client_id,
        "redirect_uri": args.redirect_uri,
    })
    print("\n1) Open this URL in your browser, log in, and approve:\n")
    print(f"   {AUTH_URL}?{q}\n")
    print("2) Box redirects to your redirect URI with `?code=...` in the URL "
          "bar.\n   Copy ONLY the code value (it expires in ~30s — be quick).\n")
    code = input("Paste the code here: ").strip()
    if not code:
        print("no code provided", file=sys.stderr)
        return 2

    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "redirect_uri": args.redirect_uri,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        print(f"token exchange failed: {e.code} {e.read()[:300]!r}", file=sys.stderr)
        return 1

    refresh = body.get("refresh_token")
    if not refresh:
        print(f"no refresh_token in response: {body}", file=sys.stderr)
        return 1

    out = {
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "refresh_token": refresh,
        "token_store": args.token_store,
    }
    with open(args.out, "w") as f:
        json.dump(out, f)
    os.chmod(args.out, 0o600)
    # Seed the rotating store too.
    os.makedirs(os.path.dirname(args.token_store), exist_ok=True)
    with open(args.token_store, "w") as f:
        json.dump({"refresh_token": refresh}, f)
    os.chmod(args.token_store, 0o600)
    print(f"\n✓ Durable OAuth config written to {args.out}")
    print("  Refresh token valid ~60 days, auto-rotates on use. No ITS needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
