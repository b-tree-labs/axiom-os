# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""`axi cred` — personal credential fabric CLI (put/get/list/rm).

``python -m axiom.extensions.builtins.cred.cli list``
"""

from __future__ import annotations

import argparse
import json
from typing import Optional

from axiom.extensions.builtins.cred.store import CredStore, MfaRequired, PostureError


def _store():
    return CredStore()


def _cmd_put(args) -> int:
    _store().put(args.name, args.value, min_posture=args.min_posture, require_mfa=args.require_mfa)
    print(f"stored '{args.name}' (floor={args.min_posture}, mfa={args.require_mfa})")
    return 0


def _cmd_get(args) -> int:
    from axiom.infra.principal import resolve_principal

    try:
        value = _store().get(args.name, principal=resolve_principal())
    except KeyError:
        print(f"no such credential '{args.name}'")
        return 1
    except (PostureError, MfaRequired) as exc:
        print(f"denied: {exc}")
        return 1
    print(value)
    return 0


def _cmd_list(_args) -> int:
    print(json.dumps(_store().list(), indent=2, sort_keys=True))
    return 0


def _cmd_rm(args) -> int:
    ok = _store().rm(args.name)
    print(f"removed '{args.name}'" if ok else f"no such credential '{args.name}'")
    return 0 if ok else 1


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="axi cred", description="Personal credential fabric.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("put", help="store a credential")
    p.add_argument("--name", required=True)
    p.add_argument("--value", required=True)
    p.add_argument("--min-posture", default="open", dest="min_posture")
    p.add_argument("--require-mfa", action="store_true", dest="require_mfa")
    p.set_defaults(func=_cmd_put)

    g = sub.add_parser("get", help="retrieve a credential (posture-gated)")
    g.add_argument("--name", required=True)
    g.set_defaults(func=_cmd_get)

    sub.add_parser("list", help="list names + floors (never values)").set_defaults(func=_cmd_list)

    r = sub.add_parser("rm", help="remove a credential")
    r.add_argument("--name", required=True)
    r.set_defaults(func=_cmd_rm)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
