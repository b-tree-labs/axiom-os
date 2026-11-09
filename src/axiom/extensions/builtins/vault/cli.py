# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi vault``: the one noun for KEEP's vault (ADR-218).

Per ADR-056, each verb is a thin wrapper over a skill and dispatches through
``invoke_capability``. The verbs come in four groups, and ``--help`` lists
them that way:

* values      set get list rm rotate exposed
* lifecycle   resolve declare issue revoke renew sweep steward
* records     audit reconcile reindex annotate, and the backup verbs
* setup       where setup discover diagnose git-credential wire-git

``issue`` and ``revoke`` are still stubs, and they say so and exit non-zero:
a verb that prints "not yet implemented" is honest, one that returns 0 is how
a heartbeat comes to report success for work nobody did.

``axi secrets <verb>`` is the deprecated spelling of the values and setup
verbs; ``axiom.extensions.builtins.secrets.cli`` maps each one onto this.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any


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


#: Every verb, in the group ``--help`` lists it under, with its one-line help.
#: The order here is the order of the help text.
VERB_GROUPS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    ("values", "the credentials themselves", (
        ("set", "store a credential (value from stdin or a hidden prompt, never argv)"),
        ("get", "a credential's metadata; --reveal prints the value (break-glass)"),
        ("list", "stored credentials with host, purposes and expiry health; --host narrows it"),
        ("rm", "delete a credential, value and metadata (confirmed)"),
        ("rotate", "rotate one credential now; --force is the leaked-key closer"),
        ("exposed", "a credential leaked: record where, then force-rotate it"),
    )),
    ("lifecycle", "which credential, and keeping it alive", (
        ("resolve", "which stored credential to use for a host, and whether it is alive"),
        ("declare", "record what can be done about a credential (its disposition)"),
        ("issue", "issue a capability (not yet implemented)"),
        ("revoke", "revoke a capability (not yet implemented)"),
        ("renew", "rotate what is due and allowed to rotate itself"),
        ("sweep", "look for credential material outside the vault"),
        ("steward", "KEEP's heartbeat: sweep, renew and reconcile"),
    )),
    ("records", "what the vault knows and has done", (
        ("audit", "expiry and access, in two sections (--expiry, --access)"),
        ("reconcile", "compare what we recorded with what each issuer says"),
        ("reindex", "bring the credential index up to date; backfill fingerprints"),
        ("annotate", "record expiry, host or notes without replacing the value"),
        ("backup", "take an encrypted backup (also: ls show verify restore prune pin unpin "
                   "scrub key-info key-init)"),
    )),
    ("setup", "where the vault is, and wiring things to it", (
        ("where", "where this machine's vault is and how each setting resolves"),
        ("setup", "start this computer's local vault and point your shells at it"),
        ("discover", "find credential material living outside the vault"),
        ("diagnose", "probe a secret reference, or every backend"),
        ("git-credential", "the git credential-helper protocol (get, store, erase)"),
        ("wire-git", "point git's credential lookup for a host at a stored credential"),
    )),
)

#: Verbs a CLI invocation can name, besides the backup verbs.
VERBS: tuple[str, ...] = tuple(v for _, _, verbs in VERB_GROUPS for v, _ in verbs if v != "backup")

#: Verbs whose skill lives in the storage module; dispatched by ``_storage_verb``.
_STORAGE_VERBS = frozenset({
    "set", "get", "list", "rm", "rotate", "exposed", "reindex", "annotate",
    "where", "setup", "discover", "diagnose", "git-credential", "wire-git",
})


def help_text() -> str:
    """The grouped verb listing, as ``--help`` prints it."""
    lines = []
    for group, blurb, verbs in VERB_GROUPS:
        lines.append(f"{group} ({blurb}):")
        width = max(len(v) for v, _ in verbs)
        lines.extend(f"  {v.ljust(width)}  {h}" for v, h in verbs)
        lines.append("")
    lines.append(f"Run `{_brand_cli()} vault <verb> --help` for a verb's options.")
    return "\n".join(lines)


def _json_flag(p: argparse.ArgumentParser, *, top: bool = False) -> None:
    # Accepted before or after the verb. On a sub-parser the default is
    # SUPPRESS, so an absent flag there does not overwrite one given before.
    p.add_argument("--json", action="store_true",
                   default=False if top else argparse.SUPPRESS,
                   help="emit the result as JSON")


def _parser() -> argparse.ArgumentParser:
    cli = _brand_cli()
    p = argparse.ArgumentParser(
        prog=f"{cli} vault",
        description="KEEP's vault: credentials, their lifecycle, the records, and setup.",
        epilog=help_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _json_flag(p, top=True)
    sub = p.add_subparsers(dest="subcommand", metavar="<verb>", help=argparse.SUPPRESS)
    helps = {v: h for _, _, verbs in VERB_GROUPS for v, h in verbs}

    def verb(name: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=helps.get(name), description=helps.get(name))
        _json_flag(sp)
        return sp

    # -- values --------------------------------------------------------------
    st = verb("set")
    st.add_argument("name", help="Credential name (e.g. hpc-gitlab-pat).")
    st.add_argument("--provider", default=None, help="RotationProvider kind (gitlab-pat, guided).")
    st.add_argument("--issuer-url", default=None, help="Issuer base URL (e.g. https://gitlab.example.org).")
    st.add_argument("--host", default=None,
                    help="The host this credential is for, when no issuer URL says it.")
    st.add_argument("--expires-at", default=None, help=f"Expiry (YYYY-MM-DD) for `{cli} vault audit`.")
    st.add_argument("--notes", default=None, help="Free-form note.")
    st.add_argument("--auto-rotate", action="store_true", help="KEEP may rotate this credential unattended.")
    st.add_argument("--no-auto-rotate", action="store_true",
                    help="KEEP must not rotate this credential unattended.")
    st.add_argument("--client-id", dest="client_id", default=None,
                    help="OAuth/OIDC client id this secret belongs to (public).")

    gt = verb("get")
    gt.add_argument("name")
    gt.add_argument("--reveal", action="store_true", help="Print the secret value (warning attached).")

    ls = verb("list")
    ls.add_argument("--host", default=None,
                    help="Only the credentials for this host: exactly the ones `resolve --host` considers.")

    rm = verb("rm")
    rm.add_argument("name")
    rm.add_argument("--yes", action="store_true", help="Skip the interactive confirmation.")

    rot = verb("rotate")
    rot.add_argument("ref", help="SecretRef (e.g. openbao://kv/data/x) or a stored credential name.")
    rot.add_argument("--provider", default=None, help="Stored names only: RotationProvider kind.")
    rot.add_argument("--issuer-url", default=None, help="Stored names only: issuer base URL override.")
    rot.add_argument("--expires-at", default=None,
                     help="Stored names only: expiry for the replacement (YYYY-MM-DD). If omitted, a recorded "
                          "expiry is kept. auto_rotate, or a credential with no recorded expiry, uses one week.")
    rot.add_argument("--strategy", default="provider-native",
                     help="provider-native (the backend rotates itself) or hitl (you supply the new value).")
    rot.add_argument("--auto-rotate", action="store_true",
                     help="Opt this credential into unattended rotation, and use the one-week expiry.")
    rot.add_argument("--no-auto-rotate", action="store_true", help="Opt this credential out of unattended rotation.")
    rot.add_argument("--force", action="store_true", help="Rotate regardless of cadence: the leaked-key closer.")
    rot.add_argument("--value", default=None, help="hitl only: the new value. Omit to be prompted.")
    rot.add_argument("--overlap", type=int, default=0,
                     help="Seconds the previous credential stays valid before retirement (default 0).")
    rot.add_argument("--cadence", type=int, default=None, help="Rotation cadence in seconds.")

    exp = verb("exposed")
    exp.add_argument("ref", help="The credential that was exposed (a SecretRef or a stored name).")
    exp.add_argument("--where", required=True, help="Where it appeared: transcript, log, chat, url, ...")
    exp.add_argument("--detail", default=None, help="Context for the audit trail (session id, file, link).")
    exp.add_argument("--strategy", default="provider-native", help="provider-native or hitl.")
    exp.add_argument("--value", default=None, help="hitl only: the new value. Omit to be prompted.")
    exp.add_argument("--overlap", type=int, default=0,
                     help="Seconds before the leaked credential retires (default 0: now).")

    # -- lifecycle -----------------------------------------------------------
    rs = verb("resolve")
    rs.add_argument("--host", default=None, help="The host to resolve a credential for.")
    rs.add_argument("--purpose", default=None,
                    help="git|api|registry|runner. Omit to list every candidate rather than have one chosen.")
    rs.add_argument("--probe", action="store_true",
                    help="Test each candidate against its issuer. One credential failing is not the host failing.")

    dc = verb("declare")
    dc.add_argument("positional_name", nargs="?", default=None, metavar="name",
                    help="The credential to classify (or --name).")
    dc.add_argument("--name", default=None, help=argparse.SUPPRESS)
    dc.add_argument("--disposition", default=None,
                    help="self_rotatable | human_rotatable | externally_owned | non_expiring.")
    dc.add_argument("--review-by", default=None, help="When to look at this again (YYYY-MM-DD).")
    dc.add_argument("--owner", default=None, help="Who to contact for an externally-owned credential.")

    verb("issue")
    verb("revoke")

    for name in ("renew", "steward"):
        rn = verb(name)
        rn.add_argument("--horizon-days", type=int, default=None,
                        help="Treat credentials expiring within N days as due (default 3).")
        rn.add_argument("--dry-run", action="store_true",
                        help="Report what would rotate without touching any credential.")
        if name == "steward":
            rn.add_argument("--workspace", default=None, help="Root to scan for git remotes.")
            rn.add_argument("--env-root", default=None, help="Root to scan for env files.")

    sw = verb("sweep")
    sw.add_argument("--workspace", default=None, help="Root to scan for git remotes (default ~/Projects).")
    sw.add_argument("--env-root", default=None, help="Root to scan for env files (default ~/.config).")

    # -- records -------------------------------------------------------------
    au = verb("audit")
    au.add_argument("--expiry", action="store_true",
                    help="The expiry section: every credential's health, flagged ones listed.")
    au.add_argument("--access", action="store_true",
                    help="The access section: who called the vault's verbs, over which surface.")
    au.add_argument("--within-days", type=int, default=None, help="Expiring-soon horizon (default 14).")
    # The deprecated `secrets audit` was a report that never failed; its alias
    # passes this so scripts written against it keep their exit code.
    au.add_argument("--report-only", action="store_true", help=argparse.SUPPRESS)

    rc = verb("reconcile")
    rc.add_argument("--apply", action="store_true",
                    help="Record the issuer's expiry locally. Metadata only, never a value.")

    ri = verb("reindex")
    ri.add_argument("--dry-run", action="store_true", help="Report what would change, open nothing.")

    an = verb("annotate")
    an.add_argument("name")
    an.add_argument("--expires-at", default=None, help="Expiry of the credential already stored (YYYY-MM-DD).")
    an.add_argument("--host", default=None, help="The host this credential is for.")
    an.add_argument("--notes", default=None, help="Operator note. Does not touch the secret value.")

    # -- setup ---------------------------------------------------------------
    wh = verb("where")
    wh.add_argument("--backend", default=None, help="the store for credentials, e.g. openbao")
    wh.add_argument("--url", default=None, help="the vault's address, e.g. http://127.0.0.1:8200")
    wh.add_argument("--token-file", dest="token_file", default=None,
                    help="a file readable only by you that holds the vault token")
    wh.add_argument("--mount", default=None, help="the KV mount (default kv)")

    su = verb("setup")
    su.add_argument("--shell", default=None, metavar="LIST",
                    help="shells to configure, comma separated: zsh,bash,sh,fish,pwsh,powershell,nu,cmd "
                         "(default: the one you are in)")
    su.add_argument("--all-detected", dest="all_detected", action="store_true",
                    help="configure every shell found on this computer")
    su.add_argument("--yes", action="store_true",
                    help="edit the profiles without asking. Without a terminal and without --yes nothing is "
                         "edited; the lines are printed instead")
    su.add_argument("--container", default=None, help="container name (default axi-openbao)")
    su.add_argument("--port", default=None, help="local port (default 8200)")

    ds = verb("discover")
    ds.add_argument("--workspace", default=None, help="Root to scan for git remotes (default ~/Projects).")
    ds.add_argument("--env-root", default=None, help="Root to scan for env files (default ~/.config).")
    ds.add_argument("--probe", action="append", dest="probes", default=None,
                    help="Limit to a probe (repeatable): git-remote, git-credentials, env-file.")

    dg = verb("diagnose")
    dg.add_argument("ref", nargs="?", default=None,
                    help="SecretRef to probe (e.g. openbao://kv/data/x or env://NAME). Omit to walk all kinds.")

    gc = verb("git-credential")
    gc.add_argument("op", choices=["get", "store", "erase"])

    wg = verb("wire-git")
    wg.add_argument("name", help="Stored credential to wire.")
    wg.add_argument("--host", required=True, help="Git host (e.g. gitlab.example.org).")
    wg.add_argument("--username", default=None, help="Username to hand git (default: token).")
    wg.add_argument("--config-file", default=None, help="Write to this git config file instead of --global.")

    return p


def cli() -> int:
    """The ``axi vault`` entry point."""
    return main(sys.argv[1:])


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv`` (default ``sys.argv[1:]``) and run the verb."""
    from .backup_cli import VERBS as _BACKUP_VERBS

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in _BACKUP_VERBS:
        from .backup_cli import main as _backup_main

        return _backup_main(argv)
    args = _parser().parse_args(argv)
    if not args.subcommand:
        _parser().print_help()
        return 2
    return main_with_args(args)


def _build_ctx():
    """The context every skill is entitled to assume it has.

    KEEP's CLI once handed its skills a literal ``None``, and the skills
    dereference ``ctx.state_dir`` unconditionally, so ``renew`` and ``steward``
    died on ``'NoneType' object has no attribute 'state_dir'``. Building the
    context here is what makes the contract true.
    """
    import logging

    from axiom.extensions.builtins.secrets import providers as _providers  # noqa: F401  (registers kinds)
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
    """Dispatch a parsed namespace. Separate from :func:`main` so the verbs are
    reachable from a test without going through ``sys.argv``."""
    verb = getattr(args, "subcommand", None)
    if verb in _STORAGE_VERBS:
        return _storage_verb(verb, args)
    if verb == "sweep":
        return _sweep(args)
    if verb == "renew":
        return _renew(args)
    if verb == "steward":
        return _steward(args)
    if verb == "resolve":
        return _capability("vault.resolve", args, {
            "host": getattr(args, "host", None), "purpose": getattr(args, "purpose", None),
            "probe": getattr(args, "probe", False) or None,
        })
    if verb == "reconcile":
        return _capability("vault.reconcile", args, {"apply": getattr(args, "apply", False) or None})
    if verb == "declare":
        return _capability("vault.declare", args, {
            "name": getattr(args, "name", None) or getattr(args, "positional_name", None),
            "disposition": getattr(args, "disposition", None),
            "review_by": getattr(args, "review_by", None), "owner": getattr(args, "owner", None),
        })
    if verb == "audit":
        return _capability("vault.audit", args, {
            "within_days": getattr(args, "within_days", None),
            "expiry": True if getattr(args, "expiry", False) else None,
            "access": True if getattr(args, "access", False) else None,
            "report_only": True if getattr(args, "report_only", False) else None,
        })

    print(
        f"{_brand_cli()} vault {verb or '(no subcommand)'}: not yet "
        "implemented — see prd-axiom-vault §5.2."
    )
    # NOT zero. A stub that exits 0 tells every caller the work was done.
    return 2


# --------------------------------------------------------------------------- values + setup


def _args_to_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for k, v in vars(args).items():
        if k in ("subcommand", "json") or v is None:
            continue
        params[k.replace("-", "_")] = v
    return params


def _read_value_for_set() -> str | None:
    """Value ingestion for ``set``: piped stdin or masked prompt. Never argv."""
    if not sys.stdin.isatty():
        return sys.stdin.read().rstrip("\n") or None
    try:
        return getpass.getpass("Secret value (input hidden): ") or None
    except (EOFError, KeyboardInterrupt):
        return None


def _emit(result, as_json: bool) -> int:
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


def _storage_verb(verb: str, args) -> int:
    from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability

    ctx = _build_ctx()
    params = _args_to_params(args)
    skill = f"vault.{verb.replace('-', '_')}"

    if verb == "setup":
        params["interactive"] = sys.stdin.isatty() and sys.stdout.isatty()

    if verb == "set":
        value = _read_value_for_set()
        if value is not None:
            params["value"] = value

    if verb == "git-credential":
        # git credential protocol: key=value request lines on stdin,
        # raw protocol lines on stdout — no JSON, no decoration.
        params["input"] = sys.stdin.read() if not sys.stdin.isatty() else ""
        result = invoke_capability(ctx.registry, skill, params, ctx, surface=CLI_SURFACE)
        if result.ok:
            out = (result.value or {}).get("output", "")
            if out:
                sys.stdout.write(out)
        else:
            for err in result.errors:
                print(f"ERROR: {err}", file=sys.stderr)
        return result.exit_code

    result = invoke_capability(ctx.registry, skill, params, ctx, surface=CLI_SURFACE)

    if verb == "get" and result.ok and isinstance(result.value, dict) and "value" in result.value:
        # Explicit --reveal: warning on stderr, the value alone on stdout.
        print(result.value["warning"], file=sys.stderr)
        print(result.value["value"])
        return result.exit_code

    return _emit(result, getattr(args, "json", False))


# --------------------------------------------------------------------------- lifecycle + records


def _print_access(section: dict) -> None:
    rows = section.get("verbs") or []
    if section.get("error"):
        print(f"    {section['error']}")
    if not rows:
        print("    no vault calls in the access record")
    for row in rows:
        surfaces = ", ".join(f"{k} {v}" for k, v in sorted((row.get("surfaces") or {}).items()))
        who = ", ".join(row.get("principals") or []) or "caller not recorded"
        failed = f", {row['failures']} failed" if row.get("failures") else ""
        print(f"    {row.get('verb', '?')}: {row.get('calls', 0)} call(s){failed}; "
              f"{surfaces}; last {row.get('last_at', '?')}; {who}")
    if section.get("note"):
        print(f"    (partial: {section['note']})")


def _capability(name: str, args, params: dict) -> int:
    """Run a lifecycle or records verb through the dispatch chokepoint and print its report."""
    import json as _json

    from axiom.infra.skill_dispatch import invoke_capability

    ctx = _build_ctx()
    result = invoke_capability(
        ctx.registry, name, {k: v for k, v in params.items() if v is not None},
        ctx, surface="cli",
    )
    value = result.value or {}
    if getattr(args, "json", False):
        print(_json.dumps({"ok": result.ok, "value": result.value,
                           "errors": result.errors}, indent=2, default=str))
        return 0 if result.ok else 1
    sections = value.get("sections") or []
    if len(sections) > 1:
        print("Expiry")
    for action in result.actions_taken:
        print(f"  {action}")
    for row in value.get("candidates", []):
        marker = "*" if row["name"] == value.get("git_helper_would_return") else " "
        purposes = "/".join(row["purposes"]) or "purpose unlabelled"
        alive = {True: "ALIVE", False: "DEAD", None: ""}[row.get("alive")]
        detail = f"  {row.get('probe_detail')}" if row.get("probe_detail") else ""
        print(f"  {marker} {row['name']:28} {purposes:22} "
              f"{row['health']:10} {alive:5}{detail}")
    if value.get("git_helper_would_return"):
        print("  (* = what a git credential helper would return for this host)")
    # Two verbs render through here and they do NOT share a finding shape.
    # `vault.reconcile` compares us against the issuer, so its rows carry
    # `status` / `stored` / `issuer`. `vault.audit` only reads recorded expiry,
    # so its rows carry `level` / `expires_at` / `detail`. Dispatch on the
    # shape rather than on the verb name: a finding is data from another
    # skill, and a key it never promised must not be load-bearing here.
    for row in value.get("findings", []):
        name_ = str(row.get("name", "?"))
        if "status" in row:  # vault.reconcile
            if row["status"] in ("ok", "unsupported", "expiry_unknown_everywhere"):
                continue
            stored = (row.get("stored") or {}).get("expires_at") or "-"
            issuer = (row.get("issuer") or {}).get("expires_at") or "-"
            print(f"    {name_:28} {row['status']:24} "
                  f"stored={stored:12} issuer={issuer}")
        else:  # vault.audit
            # An absent expiry is WRITTEN, never rendered as the string "None".
            expires = row.get("expires_at") or "expiry not recorded"
            detail = f"  {row['detail']}" if row.get("detail") else ""
            print(f"    {name_:28} {str(row.get('level', '?')):24} "
                  f"expires={expires}{detail}")
    if "access" in sections:
        if len(sections) > 1:
            print("")
            print("Access")
        _print_access(value.get("access") or {})
    for err in result.errors:
        print(f"  {err}")
    return 0 if result.ok else 1


def _sweep(args) -> int:
    """KEEP's stewardship pass — see skills/sweep.py for the KEEP/storage split."""
    import json as _json

    from axiom.infra.skill_dispatch import invoke_capability

    params = {k: v for k, v in (("workspace", getattr(args, "workspace", None)),
                                ("env_root", getattr(args, "env_root", None))) if v}
    ctx = _build_ctx()
    result = invoke_capability(ctx.registry, "vault.sweep", params, ctx, surface="cli")
    if getattr(args, "json", False):
        print(_json.dumps({"ok": result.ok, "value": result.value,
                           "errors": result.errors}, indent=2, default=str))
    else:
        for action in result.actions_taken:
            print(f"  {action}")
        for err in result.errors:
            print(f"  {err}")
        # Three outcomes, not two. "could not check" is its own answer and
        # must not borrow either of the others' words.
        if result.ok:
            print("KEEP sweep: clean")
        elif (result.value or {}).get("unverifiable"):
            print("KEEP sweep: COULD NOT VERIFY — "
                  f"{(result.value or {}).get('total', 0)} finding(s) and no "
                  "fingerprint index to check them against")
        else:
            print("KEEP sweep: UNMANAGED CREDENTIALS FOUND")
    return 0 if result.ok else 1


def _renew(args) -> int:
    """KEEP's expiry-rotation pass — see skills/renew.py for the policy."""
    import json as _json

    from axiom.infra.skill_dispatch import invoke_capability

    params: dict = {"dry_run": bool(getattr(args, "dry_run", False))}
    if getattr(args, "horizon_days", None) is not None:
        params["horizon_days"] = args.horizon_days
    ctx = _build_ctx()
    result = invoke_capability(ctx.registry, "vault.renew", params, ctx, surface="cli")
    if getattr(args, "json", False):
        print(_json.dumps({"ok": result.ok, "value": result.value,
                           "errors": result.errors}, indent=2, default=str))
    else:
        for action in result.actions_taken:
            print(f"  {action}")
        for err in result.errors:
            print(f"  {err}")
        print("KEEP renew: nothing needs a human" if result.ok
              else "KEEP renew: CREDENTIALS NEED ATTENTION")
    return 0 if result.ok else 1


def _steward(args) -> int:
    """KEEP's full stewardship pass: sweep + renew + reconcile.

    This is the heartbeat surface. All three always run: an unmanaged
    credential, an expiring one and one dead at its issuer are independent
    failures, and stopping at the first would hide the others. Reconcile is
    read-only here: the steward proposes, it does not rewrite metadata behind
    anybody.
    """
    sweep_rc = _sweep(args)
    renew_rc = _renew(args)
    reconcile_rc = _capability("vault.reconcile", args, {})
    return 0 if (sweep_rc == 0 and renew_rc == 0 and reconcile_rc == 0) else 1


__all__ = ["VERBS", "VERB_GROUPS", "cli", "help_text", "main", "main_with_args"]


if __name__ == "__main__":
    raise SystemExit(cli())
