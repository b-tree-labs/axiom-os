# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi vault backup | ls | show | verify | restore | prune | pin | unpin | scrub | key-info | key-init``.

Thin wrappers (ADR-056) over the ``vault.backup*`` skills.  This layer owns the two things a
skill must not: reading the recovery code (hidden prompt or a 0600 file, never argv, never env)
and showing a freshly minted one exactly once (terminal on stderr, or a new 0600 file).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import sys

VERBS: dict[str, str] = {
    "backup": "vault.backup",
    "ls": "vault.backup_ls",
    "show": "vault.backup_show",
    "verify": "vault.backup_verify",
    "restore": "vault.backup_restore",
    "prune": "vault.backup_prune",
    "pin": "vault.backup_pin",
    "unpin": "vault.backup_unpin",
    "scrub": "vault.backup_scrub",
    "key-info": "vault.key_info",
    "key-init": "vault.key_init",
}


def _brand_cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def _parser(verb: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"{_brand_cli()} vault {verb}")
    p.add_argument("--json", action="store_true")
    if verb in ("show", "restore", "pin", "unpin", "verify"):
        p.add_argument("id", nargs="?", default=None, help="backup id, a unique prefix, or 'latest'")
    if verb in ("backup", "prune", "ls", "show", "verify", "restore", "scrub", "key-info", "pin", "unpin"):
        p.add_argument("--to", "--from", dest="to", default=None, metavar="DIR",
                       help="backup directory (default: <state>/vault-backups); e.g. an external disk")
    if verb in ("verify", "restore", "scrub", "backup", "key-init"):
        p.add_argument("--code-file", default=None, metavar="FILE",
                       help="read the recovery code / passphrase from this file (keep it mode 0600)")
    if verb == "backup":
        p.add_argument("--force", action="store_true", help="write a backup even if nothing changed")
        p.add_argument("--passphrase", action="store_true", help="encrypt to a passphrase you type instead of the key")
        p.add_argument("--reason", default=None)
    if verb == "verify":
        p.add_argument("--deep", action="store_true", help="also decrypt and compare fingerprints")
    if verb == "restore":
        p.add_argument("--only", nargs="+", default=None, metavar="NAME", help="names or globs (file:* for key files)")
        p.add_argument("--apply", action="store_true", help="really write (default is a dry run)")
        p.add_argument("--overwrite", action="store_true", help="replace values that differ from the backup")
        p.add_argument("--include-tainted", action="store_true", help="restore values reported exposed (dangerous)")
        p.add_argument("--skip-pre-backup", action="store_true",
                       help="restore even if the safety backup of the current vault cannot be made (no undo)")
        p.add_argument("--force", action="store_true",
                       help="restore 'latest' from a directory whose ledger looks rolled back or emptied")
    if verb == "prune":
        p.add_argument("--apply", action="store_true", help="really delete (default is a dry run)")
    if verb == "scrub":
        p.add_argument("--name", required=True, help="the credential whose old values to purge from backups")
        p.add_argument("--all-versions", action="store_true", help="remove every stored version, not only exposed ones")
    if verb == "key-init":
        p.add_argument("--passphrase", action="store_true", help="choose your own passphrase instead of a generated code")
        p.add_argument("--code-out", default=None, metavar="FILE", help="write the generated code to a NEW 0600 file")
        p.add_argument("--force", action="store_true", help="replace an existing key (old backups need the old code)")
    return p


def _read_code_file(path: str) -> str:
    st = os.stat(path)
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(f"warning: {path} is readable by others; chmod 600 it", file=sys.stderr)
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def _unlock_supplier(args, prompt: str):
    """A zero-arg callable (asked only if the skill needs it), or None when nobody can be asked."""
    if getattr(args, "code_file", None):
        path = args.code_file
        return lambda: _read_code_file(path)
    if sys.stdin.isatty():
        return lambda: getpass.getpass(prompt)
    return None


def _new_passphrase() -> str:
    a = getpass.getpass("Choose a passphrase (12+ characters): ")
    b = getpass.getpass("Again: ")
    if a != b:
        raise SystemExit("passphrases did not match; nothing was created")
    return a


def _reveal_factory(args):
    def reveal(code: str) -> None:
        if args.code_out:
            fd = os.open(args.code_out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, (code + "\n").encode())
            finally:
                os.close(fd)
            print(f"The recovery code was written to {args.code_out} (mode 0600). Move it OFFLINE now, then "
                  "delete this file. It is not stored anywhere else.", file=sys.stderr)
            return
        err = sys.stderr
        print("", file=err)
        print("  RECOVERY CODE  (shown once; it is not stored and cannot be shown again)", file=err)
        print(f"      {code}", file=err)
        print("", file=err)
        print("  Print it or write it down and keep it OFFLINE, apart from this machine and from your backups.",
              file=err)
        print("  Without it, the backups cannot be restored. Anyone who has it AND a backup can read it.", file=err)
        if sys.stdin.isatty():
            tail = code.split("-")[-1]
            for _ in range(3):
                if input(f"  Type the last group ({'*' * 4}) to confirm you recorded it: ").strip().upper() == tail:
                    break
            else:
                print("  not confirmed; the key exists. Re-create it with --force if you lost the code.", file=err)
    return reveal


def main(argv: list[str]) -> int:
    verb, rest = argv[0], argv[1:]
    args = _parser(verb).parse_args(rest)
    params: dict = {}
    if getattr(args, "id", None):
        params["id"] = args.id
    if getattr(args, "to", None):
        params["to"] = args.to
    for k in ("force", "apply", "overwrite", "include_tainted", "skip_pre_backup", "deep", "all_versions", "reason", "name"):
        if getattr(args, k, None):
            params[k] = getattr(args, k)
    if getattr(args, "only", None):
        params["only"] = args.only

    if verb == "backup" and args.passphrase:
        params["passphrase"] = True
        params["unlock_secret"] = _unlock_supplier(args, "Passphrase for this backup: ")
    elif verb in ("restore", "scrub") or (verb == "verify" and args.deep):
        params["unlock_secret"] = _unlock_supplier(args, "Recovery code or passphrase: ")
    elif verb == "key-init":
        if args.passphrase:
            if not sys.stdin.isatty():
                print("key-init --passphrase needs a terminal to ask in; nothing was created.", file=sys.stderr)
                return 1
            params["passphrase"] = True
            params["unlock_secret"] = _new_passphrase()
        else:
            params["reveal"] = _reveal_factory(args)
        if args.force:
            params["force"] = True

    from axiom.extensions.builtins.vault.cli import _build_ctx
    from axiom.infra.skill_dispatch import invoke_capability

    ctx = _build_ctx()
    result = invoke_capability(ctx.registry, VERBS[verb], params, ctx, surface="cli")
    value = result.value or {}
    if args.json:
        print(json.dumps({"ok": result.ok, "value": value, "errors": list(result.errors)}, indent=2, default=str))
        return 0 if result.ok else 1
    for line in result.actions_taken:
        print(line)
    _render(verb, value)
    for err in result.errors:
        print(f"error: {err}", file=sys.stderr)
    steps = value.get("next_steps") or []
    if steps:
        print("\nNext:")
        for s in steps:
            print(f"  {s}")
    return 0 if result.ok else 1


def _render(verb: str, v: dict) -> None:
    if verb == "ls":
        for b in v.get("backups", []):
            flags = ",".join(x for x in ("PINNED" if b["pinned"] else "", "TAINTED" if b["tainted"] else "",
                                         "" if b["status"] == "ok" else b["status"].upper()) if x)
            print(f"  {b['id']}  {b['created_at']}  {b['count']:>3} entries  {b['reason'][:28]:28}  {flags}")
    elif verb == "show":
        for e in v.get("entries", []):
            note = "  [EXPOSED]" if e.get("tainted") else ""
            print(f"  {e['name']:34} {e.get('scope', ''):14} {e.get('fingerprint', '')}{note}")
    elif verb == "prune":
        for key in ("delete", "keep"):
            for d in v.get(key, []):
                print(f"  {key:6} {d['id']}  {', '.join(d['reasons'])}")
    elif verb == "key-info":
        for k in ("root", "second_copy"):
            if v.get(k):
                print(f"  {k}: {v[k]}")
