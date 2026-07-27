# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi gate`` — administer the gate's accounts and API keys.

Per ADR-056 the verbs here are thin wrappers: translate flags → a params dict
and dispatch to ``SkillRegistry.invoke``. All logic lives in the ``gate.*``
skill functions (see ``skills/``). The accounts file is chosen by
``--accounts-file`` or ``$AXIOM_GATE_USERS_FILE``; the API-keys file by
``--keys-file`` or ``$AXIOM_GATE_API_KEYS_FILE``. Writes are picked up live
(mtime hot-reload) — accounts on the next login, API keys on the next
request; no restart.

Verb grammar per AEOS §4.3.1: imperative verb, resource positional —
``axi gate issue api-key --principal @svc:context --scope llm``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from axiom.infra.paths import get_user_state_dir
from axiom.infra.skills import SkillContext, SkillResult

from . import skills as gate_skills

_PROG = "axi gate"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=_PROG,
        description="Administer the gate's password accounts and API keys.",
    )
    p.add_argument("--json", action="store_true",
                   help="emit the SkillResult as JSON")
    p.add_argument("--accounts-file",
                   help="accounts JSON file (default: $AXIOM_GATE_USERS_FILE)")
    p.add_argument("--keys-file",
                   help="API-keys JSON file (default: $AXIOM_GATE_API_KEYS_FILE)")
    sub = p.add_subparsers(dest="verb", required=True)

    add = sub.add_parser("adduser", help="Add a password account.")
    add.add_argument("email", help="the account's email (the login + identity)")
    add.add_argument("--role", action="append", metavar="ROLE",
                     help="grant a role (repeatable); e.g. --role operator")
    add.add_argument("--name", help="display name")
    add.add_argument("--password",
                     help="set the password (default: generate a strong one "
                          "and print it once)")
    add.add_argument("--user-id",
                     help="stable principal id (default: the email)")
    add.add_argument("--force", action="store_true",
                     help="overwrite an existing account with this email")

    rst = sub.add_parser("resetpw", help="Reset an existing account's password.")
    rst.add_argument("email", help="the account to reset")
    rst.add_argument("--password",
                     help="set the new password (default: generate + print once)")

    lst = sub.add_parser(
        "list", help="List accounts or API keys (never prints hashes).")
    lst.add_argument("resource", nargs="?", default="accounts",
                     choices=["accounts", "api-keys"],
                     help="what to list (default: accounts)")

    iss = sub.add_parser(
        "issue",
        help="Issue a bearer API key bound to a NON-human API principal.")
    iss.add_argument("resource", choices=["api-key"],
                     help="the credential kind to issue")
    iss.add_argument("--principal", required=True,
                     help="the service principal the key acts as "
                          "(matrix-style @name:context, e.g. @svc:site)")
    iss.add_argument("--scope", action="append", required=True,
                     metavar="MOUNT[:VERB]",
                     help="grant a scope (repeatable): a composed-app mount, "
                          "optionally narrowed to read|invoke|access "
                          "(e.g. --scope llm --scope rag:read)")
    iss.add_argument("--name", help="what this key is for (free-form note)")

    rvk = sub.add_parser(
        "revoke", help="Revoke an issued API key (effective immediately).")
    rvk.add_argument("resource", choices=["api-key"],
                     help="the credential kind to revoke")
    rvk.add_argument("key_id", help="the key id (from issue / list api-keys)")

    return p


def _build_ctx() -> SkillContext:
    return SkillContext(
        registry=gate_skills.bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.gate"),
        user_prompt=_terminal_prompt if sys.stdin.isatty() else None,
    )


def _terminal_prompt(prompt: str) -> str:
    return input(prompt)


def _args_to_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for k, v in vars(args).items():
        if k in ("verb", "json"):
            continue
        if v is None:
            continue
        params[k.replace("-", "_")] = v
    return params


def _emit(result: SkillResult, as_json: bool) -> int:
    if as_json:
        print(json.dumps({
            "ok": result.ok,
            "value": result.value,
            "errors": result.errors,
            "actions_taken": result.actions_taken,
        }, indent=2, default=str))
        return result.exit_code
    for action in result.actions_taken:
        print(f"• {action}")
    v = result.value
    if isinstance(v, dict) and "items" in v:
        for it in v["items"]:
            if "email" in it:
                roles = ", ".join(it.get("roles") or []) or "—"
                name = it.get("name") or ""
                print(f"  {it['email']:<32} {roles:<28} {name}")
            else:
                scopes = ", ".join(it.get("scopes") or []) or "—"
                state = "REVOKED" if it.get("revoked_at") else "active"
                name = it.get("name") or ""
                print(f"  {it['key_id']:<14} {it['principal']:<24} "
                      f"{scopes:<24} {state:<8} {name}")
        if not v["items"]:
            print("  (no api keys)" if "keys_file" in v else "  (no accounts)")
    if not result.ok:
        for err in result.errors:
            print(f"ERROR: {err}", file=sys.stderr)
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    ctx = _build_ctx()
    params = _args_to_params(args)
    result = ctx.registry.invoke(f"gate.{args.verb}", params, ctx)
    return _emit(result, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
