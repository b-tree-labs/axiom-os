# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi secrets`` — operator pre-flight CLI for the secrets extension.

Per ADR-056: a thin argparse wrapper that translates flags → params dict
and dispatches to ``SkillRegistry.invoke``. All business logic lives in
the skill functions under ``secrets/skills/``.

Verbs:

- ``diagnose [<ref>]`` — probe one ref end-to-end or walk every
  registered provider kind. Pre-flight only; never prints secret values.
- ``set/list/rm/get/audit`` — foreign-credential store (issue #667):
  OS-keychain values + metadata index. ``set`` takes the value from
  stdin or an interactive prompt, NEVER argv.
- ``rotate <name>`` — KEEP-executed foreign rotation (RotationProvider
  factory, guarded_act, action-ledger journal). Ref form unchanged.
- ``git-credential <get|store|erase>`` — git credential-helper protocol.
- ``wire-git <name> --host <host>`` — configure git to use it.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from typing import Any

from axiom.infra.paths import get_user_state_dir
from axiom.infra.skills import SkillContext, SkillResult

# Ensure built-in providers are registered before we ask the registry
# what kinds it knows about.
from . import providers as _providers  # noqa: F401  (import for side effect)
from . import skills as secrets_skills


_PROG = "axi secrets"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=_PROG,
        description="secrets: operator pre-flight for the SecretStore wiring.",
    )
    p.add_argument("--json", action="store_true",
                   help="emit the SkillResult as JSON")
    sub = p.add_subparsers(dest="verb", required=True)

    diag = sub.add_parser(
        "diagnose",
        help="Probe a single SecretRef end-to-end, or walk every registered kind.",
    )
    diag.add_argument(
        "ref", nargs="?", default=None,
        help="SecretRef to probe (e.g. openbao://kv/data/x or env://NAME). "
             "Omit to walk all registered kinds.",
    )

    rot = sub.add_parser(
        "rotate",
        help="Rotate one secret now (--force = the leaked-key closer). "
             "Never prints the secret value.",
    )
    rot.add_argument(
        "ref",
        help="SecretRef (e.g. openbao://kv/data/x) or a foreign credential "
             "name (e.g. hpc-gitlab-pat) for the issuer-rotation flow.",
    )
    rot.add_argument(
        "--provider", default=None,
        help="Foreign names only: RotationProvider kind (gitlab-pat, "
             "guided). Defaults to the credential's stored provider.",
    )
    rot.add_argument(
        "--issuer-url", default=None,
        help="Foreign names only: issuer base URL override "
             "(e.g. https://gitlab.example.org).",
    )
    rot.add_argument(
        "--expires-at", default=None,
        help="Foreign names only: expiry for the replacement "
             "(YYYY-MM-DD; issuer default is +1 week).",
    )
    rot.add_argument(
        "--strategy", default="provider-native",
        help="Rotation strategy: provider-native (backend rotates itself) or "
             "hitl (human supplies the new value). Default: provider-native.",
    )
    rot.add_argument(
        "--force", action="store_true",
        help="Rotate regardless of cadence — the leaked-key closer.",
    )
    rot.add_argument(
        "--value", default=None,
        help="hitl only: the new credential value. Omit to be prompted "
             "(interactive) — never pass a real secret on a shared shell history.",
    )
    rot.add_argument(
        "--overlap", type=int, default=0,
        help="Overlap window in seconds the previous credential stays valid "
             "before retirement. 0 (default) retires it inline.",
    )
    rot.add_argument(
        "--cadence", type=int, default=None,
        help="Rotation cadence in seconds (for scheduled rotation policy). "
             "Omit for force-only.",
    )

    exp = sub.add_parser(
        "exposed",
        help="A credential appeared on an observable surface (transcript, log, "
             "chat). Records the exposure, then force-rotates. Never prints "
             "the secret value.",
    )
    exp.add_argument("ref", help="SecretRef that was exposed (e.g. openbao://kv/data/x).")
    exp.add_argument(
        "--where", required=True,
        help="Surface the credential appeared on: transcript, log, chat, url, …",
    )
    exp.add_argument(
        "--detail", default=None,
        help="Optional context for the audit trail (session id, file, message link).",
    )
    exp.add_argument(
        "--strategy", default="provider-native",
        help="Rotation strategy (same as rotate): provider-native or hitl.",
    )
    exp.add_argument(
        "--value", default=None,
        help="hitl only: the new credential value. Omit to be prompted.",
    )
    exp.add_argument(
        "--overlap", type=int, default=0,
        help="Overlap window in seconds before the leaked credential retires. "
             "Default 0: retire the leaked credential inline.",
    )

    # -- foreign-credential store (issue #667) ------------------------------

    st = sub.add_parser(
        "set",
        help="Store a foreign credential (GitLab PAT, webhook URL, HMAC "
             "key). Value comes from stdin or an interactive prompt — "
             "NEVER from argv.",
    )
    st.add_argument("name", help="Credential name (e.g. hpc-gitlab-pat).")
    st.add_argument("--provider", default=None,
                    help="RotationProvider kind (gitlab-pat, guided).")
    st.add_argument("--issuer-url", default=None,
                    help="Issuer base URL (e.g. https://gitlab.example.org).")
    st.add_argument("--expires-at", default=None,
                    help="Expiry (YYYY-MM-DD) for `axi secrets audit`.")
    st.add_argument("--notes", default=None, help="Free-form note.")

    ls = sub.add_parser(
        "list", help="List foreign credentials — names + metadata, never values.",
    )
    del ls  # no extra flags

    rm_p = sub.add_parser(
        "rm", help="Delete a foreign credential (value + metadata). Confirmed.",
    )
    rm_p.add_argument("name")
    rm_p.add_argument("--yes", action="store_true",
                      help="Skip the interactive confirmation.")

    get_p = sub.add_parser(
        "get",
        help="Show a credential's metadata. --reveal prints the VALUE "
             "with a warning — break-glass only.",
    )
    get_p.add_argument("name")
    get_p.add_argument("--reveal", action="store_true",
                       help="Print the secret value (warning attached).")

    aud = sub.add_parser(
        "audit",
        help="Expiry findings over foreign credentials (expired / "
             "expiring / no_expiry). Metadata only.",
    )
    aud.add_argument("--within-days", type=int, default=None,
                     help="Expiring-soon horizon (default 14).")

    gc = sub.add_parser(
        "git-credential",
        help="git credential-helper protocol endpoint (get/store/erase); "
             "reads key=value lines on stdin, answers from the store.",
    )
    gc.add_argument("op", choices=["get", "store", "erase"])

    wg = sub.add_parser(
        "wire-git",
        help="Configure git to resolve a host's credentials from the "
             "store via the git-credential helper.",
    )
    wg.add_argument("name", help="Stored credential to wire.")
    wg.add_argument("--host", required=True,
                    help="Git host (e.g. gitlab.example.org).")
    wg.add_argument("--username", default=None,
                    help="Username to hand git (default: token).")
    wg.add_argument("--config-file", default=None,
                    help="Write to this git config file instead of --global.")

    return p


def _terminal_prompt(prompt: str) -> str:
    return input(prompt)


def _build_ctx() -> SkillContext:
    return SkillContext(
        registry=secrets_skills.bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.secrets"),
        user_prompt=_terminal_prompt if sys.stdin.isatty() else None,
    )


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
    if result.value is not None:
        if isinstance(result.value, dict) and "items" in result.value:
            for item in result.value["items"]:
                print("  " + "  ".join(f"{k}={v}" for k, v in item.items()))
        elif isinstance(result.value, (str, int, float, bool)):
            print(result.value)
        else:
            print(json.dumps(result.value, indent=2, default=str))
    if not result.ok:
        for err in result.errors:
            print(f"ERROR: {err}", file=sys.stderr)
    return result.exit_code


def _read_value_for_set() -> str | None:
    """Value ingestion for ``set``: piped stdin or masked prompt. Never argv."""
    if not sys.stdin.isatty():
        return sys.stdin.read().rstrip("\n") or None
    try:
        return getpass.getpass("Secret value (input hidden): ") or None
    except (EOFError, KeyboardInterrupt):
        return None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    ctx = _build_ctx()
    params = _args_to_params(args)
    skill = f"secrets.{args.verb.replace('-', '_')}"

    if args.verb == "set":
        value = _read_value_for_set()
        if value is not None:
            params["value"] = value

    if args.verb == "git-credential":
        # git credential protocol: key=value request lines on stdin,
        # raw protocol lines on stdout — no JSON, no decoration.
        params["input"] = sys.stdin.read() if not sys.stdin.isatty() else ""
        result = ctx.registry.invoke(skill, params, ctx)
        if result.ok:
            out = (result.value or {}).get("output", "")
            if out:
                sys.stdout.write(out)
        else:
            for err in result.errors:
                print(f"ERROR: {err}", file=sys.stderr)
        return result.exit_code

    result = ctx.registry.invoke(skill, params, ctx)

    if (
        args.verb == "get"
        and result.ok
        and isinstance(result.value, dict)
        and "value" in result.value
    ):
        # Explicit --reveal: warning on stderr, the value alone on stdout.
        print(result.value["warning"], file=sys.stderr)
        print(result.value["value"])
        return result.exit_code

    return _emit(result, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
