# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi vault`` CLI noun for KEEP.

Per ADR-056: CLI verbs are thin wrappers over skill functions.
``sweep`` is skill-backed and live. list / issue / revoke / audit remain the
Phase-1 stubs from the manifest declaration and still say so when invoked —
a verb that prints "not yet implemented" is honest; one that silently returns 0
is how a heartbeat comes to report success for work nobody did.
"""

from __future__ import annotations

import argparse


def _brand_cli() -> str:
    """The command the operator actually typed.

    These lines said "axi" unconditionally, so a consumer distribution's CLI
    told the operator to run a command that does not exist on their machine.
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def cli() -> int:
    """Vault subcommand entrypoint (Phase 1 stub)."""
    parser = argparse.ArgumentParser(
        prog=f"{_brand_cli()} vault",
        description="KEEP vault — capability lifecycle + audit.",
    )
    parser.add_argument(
        "subcommand",
        nargs="?",
        choices=["list", "issue", "revoke", "audit", "resolve", "reconcile",
                 "declare", "sweep", "renew", "steward"],
    )
    parser.add_argument("--host", default=None,
                        help="resolve: the host to resolve a credential for.")
    parser.add_argument("--purpose", default=None,
                        help="resolve: git|api|registry|runner. Omit to list every "
                             "candidate rather than have one silently chosen.")
    parser.add_argument("--probe", action="store_true",
                        help="resolve: actually TEST each candidate against its "
                             "issuer. One credential failing is not the host "
                             "failing, and this is what tells them apart.")
    parser.add_argument("--name", default=None,
                        help="declare: the credential to classify.")
    parser.add_argument("--disposition", default=None,
                        help="declare: self_rotatable | human_rotatable | "
                             "externally_owned | non_expiring.")
    parser.add_argument("--review-by", default=None,
                        help="declare: when to look at this again (YYYY-MM-DD), "
                             "for credentials with no expiry to record.")
    parser.add_argument("--owner", default=None,
                        help="declare: who to contact for an externally-owned "
                             "credential.")
    parser.add_argument("--apply", action="store_true",
                        help="reconcile: record the issuer's expiry locally. "
                             "Metadata only — never a credential value.")
    parser.add_argument("--within-days", type=int, default=None,
                        help="audit: expiry horizon in days (default 14).")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--env-root", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--horizon-days", type=int, default=None,
        help="Treat credentials expiring within N days as due (default 3).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would rotate without touching any credential.",
    )
    return main_with_args(parser.parse_args())


def _build_ctx():
    """The context every skill is entitled to assume it has.

    Every other extension's CLI builds one of these. KEEP's did not — it handed
    its skills a literal ``None``, and the skills dereference ``ctx.state_dir``
    unconditionally, so ``renew`` and ``steward`` died on
    ``'NoneType' object has no attribute 'state_dir'``. The signature
    ``ctx: SkillContext | None = None`` says None is acceptable and it is not;
    building the context here is what makes the contract true.
    """
    import logging
    import sys

    from axiom.extensions.builtins.vault import skills as vault_skills
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillContext

    def _prompt(text: str) -> str:
        return input(text)

    return SkillContext(
        registry=vault_skills.bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.vault"),
        user_prompt=_prompt if sys.stdin.isatty() else None,
    )


def main_with_args(args) -> int:
    """Dispatch a parsed namespace. Separate from :func:`cli` so the verbs are
    reachable from a test without going through ``sys.argv``."""
    if args.subcommand == "sweep":
        return _sweep(args)
    if args.subcommand == "renew":
        return _renew(args)
    if args.subcommand == "steward":
        return _steward(args)
    if args.subcommand == "resolve":
        return _capability("vault.resolve", args, {
            "host": args.host, "purpose": args.purpose,
            "probe": args.probe or None,
        })
    if args.subcommand == "reconcile":
        return _capability("vault.reconcile", args, {
            "apply": args.apply or None,
        })
    if args.subcommand == "declare":
        return _capability("vault.declare", args, {
            "name": args.name, "disposition": args.disposition,
            "review_by": args.review_by, "owner": args.owner,
        })
    if args.subcommand == "audit":
        return _capability("vault.audit", args, {
            "within_days": args.within_days,
        })

    print(
        f"{_brand_cli()} vault {args.subcommand or '(no subcommand)'}: not yet "
        "implemented — see prd-axiom-vault §5.2."
    )
    # NOT zero. `axi vault audit` is what an operator or a scheduled job runs to
    # find a credential about to expire; exiting 0 on a stub tells every caller
    # the audit passed. That is how an expired token stays invisible until
    # somebody hits a 401 by hand.
    return 2


def _capability(name: str, args, params: dict) -> int:
    """Run a read verb through the dispatch chokepoint and print its report."""
    import json as _json

    from axiom.infra.skill_dispatch import invoke_capability

    ctx = _build_ctx()
    result = invoke_capability(
        ctx.registry, name, {k: v for k, v in params.items() if v is not None},
        ctx, surface="cli",
    )
    if args.json:
        print(_json.dumps({"ok": result.ok, "value": result.value,
                           "errors": result.errors}, indent=2))
    else:
        for action in result.actions_taken:
            print(f"  {action}")
        for row in (result.value or {}).get("candidates", []):
            marker = "*" if row["name"] == (result.value or {}).get(
                "git_helper_would_return") else " "
            purposes = "/".join(row["purposes"]) or "purpose unlabelled"
            alive = {True: "ALIVE", False: "DEAD", None: ""}[row.get("alive")]
            detail = f"  {row.get('probe_detail')}" if row.get("probe_detail") else ""
            print(f"  {marker} {row['name']:28} {purposes:22} "
                  f"{row['health']:10} {alive:5}{detail}")
        if (result.value or {}).get("git_helper_would_return"):
            print("  (* = what a git credential helper would return for this host)")
        # Two verbs render through here and they do NOT share a finding shape.
        # `vault.reconcile` compares us against the issuer, so its rows carry
        # `status` / `stored` / `issuer`. `vault.audit` only reads recorded
        # expiry, so its rows carry `level` / `expires_at` / `detail`. This
        # branch assumed reconcile's keys, and `axi vault audit` died on
        # KeyError: 'status' — but only once something was flagged, because the
        # value carries flagged findings alone. The command worked right up
        # until it had a warning to deliver, then took the warning with it.
        #
        # Dispatch on the shape rather than on the verb name: a finding is data
        # from another extension's skill, and a key it never promised must not
        # be load-bearing here.
        for row in (result.value or {}).get("findings", []):
            name = str(row.get("name", "?"))
            if "status" in row:  # vault.reconcile
                if row["status"] in ("ok", "unsupported", "expiry_unknown_everywhere"):
                    continue
                stored = (row.get("stored") or {}).get("expires_at") or "-"
                issuer = (row.get("issuer") or {}).get("expires_at") or "-"
                print(f"    {name:28} {row['status']:24} "
                      f"stored={stored:12} issuer={issuer}")
            else:  # vault.audit
                # An absent expiry is WRITTEN, never rendered as the string
                # "None". `no_expiry` is the level that let a live token die
                # unnoticed, so it has to read as a stated fact.
                expires = row.get("expires_at") or "expiry not recorded"
                detail = f"  {row['detail']}" if row.get("detail") else ""
                print(f"    {name:28} {str(row.get('level', '?')):24} "
                      f"expires={expires}{detail}")
        for err in result.errors:
            print(f"  {err}")
    return 0 if result.ok else 1


def _sweep(args) -> int:
    """KEEP's stewardship pass — see skills/sweep.py for the KEEP/secrets split."""
    import json as _json

    from axiom.infra.skill_dispatch import invoke_capability

    params = {k: v for k, v in (("workspace", args.workspace),
                                ("env_root", args.env_root)) if v}
    ctx = _build_ctx()
    result = invoke_capability(ctx.registry, "vault.sweep", params, ctx, surface="cli")
    if args.json:
        print(_json.dumps({"ok": result.ok, "value": result.value,
                           "errors": result.errors}, indent=2))
    else:
        for action in result.actions_taken:
            print(f"  {action}")
        for err in result.errors:
            print(f"  {err}")
        print("KEEP sweep: clean" if result.ok
              else "KEEP sweep: UNMANAGED CREDENTIALS FOUND")
    return 0 if result.ok else 1


def _renew(args) -> int:
    """KEEP's expiry-rotation pass — see skills/renew.py for the policy."""
    import json as _json

    from axiom.infra.skill_dispatch import invoke_capability

    params: dict = {"dry_run": bool(args.dry_run)}
    if args.horizon_days is not None:
        params["horizon_days"] = args.horizon_days
    ctx = _build_ctx()
    result = invoke_capability(ctx.registry, "vault.renew", params, ctx, surface="cli")
    if args.json:
        print(_json.dumps({"ok": result.ok, "value": result.value,
                           "errors": result.errors}, indent=2))
    else:
        for action in result.actions_taken:
            print(f"  {action}")
        for err in result.errors:
            print(f"  {err}")
        print("KEEP renew: nothing needs a human" if result.ok
              else "KEEP renew: CREDENTIALS NEED ATTENTION")
    return 0 if result.ok else 1


def _steward(args) -> int:
    """KEEP's full stewardship pass: sweep + renew.

    This is the heartbeat surface. Both halves always run — an unmanaged
    credential and an expiring one are independent failures, and stopping at
    the first would hide the second.
    """
    sweep_rc = _sweep(args)
    renew_rc = _renew(args)
    # Reconciliation belongs in the unattended pass: expiry auditing reads our
    # own metadata, so a credential deleted at the issuer stays invisible to
    # every other check here. Read-only — the steward proposes, it does not
    # rewrite metadata behind anybody.
    reconcile_rc = _capability("vault.reconcile", args, {})
    return 0 if (sweep_rc == 0 and renew_rc == 0 and reconcile_rc == 0) else 1


if __name__ == "__main__":
    raise SystemExit(cli())
