# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""`axi calendar` CLI — connect (sign in + build + verify).

``python -m axiom.extensions.builtins.schedule.calendar.cli connect --vendor m365 \
    --tenant <id> --client-id <id>``
"""

from __future__ import annotations

import argparse
import time
from typing import Optional


class _RequestsHttp:
    def post(self, url, data):
        import requests

        resp = requests.post(url, data=data, timeout=30)
        resp.raise_for_status()
        return resp.json()


def _cmd_connect(args) -> int:
    from axiom.extensions.builtins.schedule.calendar.connect import connect_and_verify

    res = connect_and_verify(
        args.vendor, idp_http=_RequestsHttp(), client_id=args.client_id,
        tenant=args.tenant, user=args.user, calendar_id=args.calendar_id,
        prompt=print, sleep=time.sleep,
    )
    fires = ", ".join(f.isoformat() for f in res["next_fires"])
    print(f"connected {res['user']} -> {args.vendor} calendar '{res['calendar_id']}' "
          f"(verified={res['verified']}); next fires: {fires}")
    return 0 if res["verified"] else 1


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="axi calendar", description="Calendar connections.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("connect", help="sign in + connect a calendar + verify")
    c.add_argument("--vendor", choices=["m365", "google"], required=True)
    c.add_argument("--tenant", help="Entra tenant id (m365)")
    c.add_argument("--client-id", required=True)
    c.add_argument("--user", help="account UPN/email (else derived from id_token)")
    c.add_argument("--calendar-id", default="primary")
    c.set_defaults(func=_cmd_connect)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
