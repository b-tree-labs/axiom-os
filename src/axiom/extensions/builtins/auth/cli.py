# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""`axi auth` CLI — login / whoami / logout / providers (ADR-056, AUTH-6).

``python -m axiom.extensions.builtins.auth.cli providers``
"""

from __future__ import annotations

import argparse
import time
from typing import Optional

from axiom.extensions.builtins.auth import providers as idp_providers
from axiom.extensions.builtins.auth.login import login_with_device_code
from axiom.extensions.builtins.auth.token_store import (
    KeychainTokenStore,
    load_refresh_token,
    token_key,
)

_PROVIDERS = ("entra", "google")


class _RequestsHttp:
    def post(self, url, data):
        import requests

        resp = requests.post(url, data=data, timeout=30)
        resp.raise_for_status()
        return resp.json()


def _idp(args):
    if args.provider == "entra":
        if not args.tenant:
            raise SystemExit("entra requires --tenant")
        return idp_providers.entra(args.tenant)
    return idp_providers.google()


def _cmd_providers(_args) -> int:
    print("Available identity providers:")
    for name in _PROVIDERS:
        note = " (needs --tenant)" if name == "entra" else ""
        print(f"  - {name}{note}")
    return 0


def _cmd_login(args) -> int:
    result = login_with_device_code(
        idp=_idp(args), client_id=args.client_id, scopes=args.scopes.split(),
        http=_RequestsHttp(), user=args.user, prompt=print, sleep=time.sleep,
    )
    state = "stored" if result["stored"] else "no refresh token returned"
    print(f"signed in as {result['user']} ({state})")
    return 0


def _cmd_whoami(args) -> int:
    rt = load_refresh_token(args.provider, args.user, args.scopes.split())
    print(f"{args.user}@{args.provider}: {'connected' if rt else 'not signed in'}")
    return 0 if rt else 1


def _cmd_logout(args) -> int:
    KeychainTokenStore().put(token_key(args.provider, args.user, args.scopes.split()), "")
    print(f"signed out {args.user}@{args.provider}")
    return 0


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="axi auth", description="SSO / delegated auth.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("providers", help="list identity providers").set_defaults(func=_cmd_providers)

    def _common(p):
        p.add_argument("--provider", choices=_PROVIDERS, required=True)
        p.add_argument("--tenant", help="Entra directory/tenant id")
        p.add_argument("--scopes", default="openid email offline_access")

    lg = sub.add_parser("login", help="device-code sign-in")
    _common(lg)
    lg.add_argument("--user", help="account UPN/email (else derived from id_token)")
    lg.add_argument("--client-id", required=True)
    lg.set_defaults(func=_cmd_login)

    for name, fn, help_text in (
        ("whoami", _cmd_whoami, "show connection state"),
        ("logout", _cmd_logout, "remove the stored token"),
    ):
        p = sub.add_parser(name, help=help_text)
        _common(p)
        p.add_argument("--user", required=True)
        p.set_defaults(func=fn)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
