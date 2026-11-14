# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi state`` -- snapshot, inspect, verify, restore and prune harness state.

Thin wrappers over the ``state.*`` skill functions (ADR-056).  Restore and
prune are dry runs unless ``--apply`` is given.

    state snapshot [--to DIR] [--reason R] [--force] [--with-db --db-url URL]
    state ls [--to DIR]
    state show [ID]
    state diff [ID] [--only GLOB]
    state verify [ID] [--quick]
    state restore [ID] [--only GLOB] [--apply] [--force] [--emergency]
    state prune [--apply]
    state pin ID | unpin ID
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from typing import Any

from axiom.infra.skills import SkillResult


def _brand_cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"{_brand_cli()} state",
                                description="Snapshots of the harness state: take, verify, restore, age out.")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="verb")

    def common(sp, to=True):
        if to:
            sp.add_argument("--to", "--from", dest="to", default=None, metavar="DIR",
                            help="snapshot directory (e.g. on another disk); default <state>/snapshots")
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        return sp

    s = common(sub.add_parser("snapshot", help="take a snapshot now"))
    s.add_argument("--reason", default="on-demand")
    s.add_argument("--force", action="store_true", help="snapshot even if nothing changed")
    s.add_argument("--no-prune", action="store_true", help="skip the automatic retention pass")
    s.add_argument("--with-db", action="store_true", help="also dump the database via the supported path")
    s.add_argument("--db-url", default=None, help="database URL for --with-db (never resolved implicitly)")
    s.add_argument("--yes-this-database", action="store_true",
                   help="allow --with-db against the default developer address (localhost:5432)")
    common(sub.add_parser("ls", help="list snapshots"))
    sh = common(sub.add_parser("show", help="show one snapshot"))
    sh.add_argument("id", nargs="?", default="latest")
    d = common(sub.add_parser("diff", help="what a restore would change"))
    d.add_argument("id", nargs="?", default="latest")
    d.add_argument("--only", action="append", default=[], metavar="GLOB")
    v = common(sub.add_parser("verify", help="verify integrity (all, or one)"))
    v.add_argument("id", nargs="?", default=None)
    v.add_argument("--quick", action="store_true", help="skip re-hashing blob contents")
    r = common(sub.add_parser("restore", help="restore from a snapshot (dry run unless --apply)"))
    r.add_argument("id", nargs="?", default="latest")
    r.add_argument("--only", action="append", default=[], metavar="GLOB")
    r.add_argument("--apply", action="store_true", help="really write (default is a dry run)")
    r.add_argument("--force", action="store_true", help="restore despite failed verify / running services")
    r.add_argument("--emergency", action="store_true",
                   help="run the stdlib-only restore script instead of the in-process code")
    pr = common(sub.add_parser("prune", help="apply retention (dry run unless --apply)"))
    pr.add_argument("--apply", action="store_true")
    pr.add_argument("--harder", action="store_true", help="use the disk-pressure policy")
    for name in ("pin", "unpin"):
        pp = common(sub.add_parser(name, help=f"{name} a snapshot"))
        pp.add_argument("id")
    return p


def _ctx():
    from axiom.extensions.builtins.state import skills as state_skills
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillContext

    return SkillContext(
        registry=state_skills.bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.state"),
    )


def _run(verb: str, params: dict[str, Any]) -> SkillResult:
    from axiom.infra.skill_dispatch import invoke_capability

    ctx = _ctx()
    return invoke_capability(ctx.registry, f"state.{verb}", {k: v for k, v in params.items() if v not in (None, False, [])},
                             ctx, surface="cli")


def _kb(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n / 1:.1f} {unit}"
        n /= 1024
    return str(n)


def _render(verb: str, res: SkillResult) -> None:
    v = res.value or {}
    if not v:  # a failure before any report was built: errors only
        verb = ""
    for a in res.actions_taken:
        print(a)
    for w in v.get("warnings", []) if verb not in ("prune", "restore") else []:
        if not w.startswith("snapshot dir"):  # the coverage block says it once
            print(f"WARNING: {w}")
    if verb == "snapshot":
        for ex in v.get("excluded_summary", [])[:12]:
            n = "dir skipped" if ex.get("files") is None else f"{ex['files']} file(s)"
            print(f"  excluded [{ex['tier']:11}] {ex['pattern']:32} {n:14} {ex['reason']}")
        for e in v.get("scan_errors", []):
            print(f"  could not read: {e}")
    elif verb == "ls":
        print(f"{v['root']}  (every {v['interval_hours']}h, {'enabled' if v['enabled'] else 'DISABLED'})")
        if v["snapshots"]:
            from axiom.infra.cli_format import Column, table

            print(
                "\n".join(
                    table(
                        [
                            (
                                r["id"],
                                r.get("created_at") or "?",
                                f"{r.get('files', '?')} files",
                                _kb(r.get("bytes", 0)),
                                f"{r.get('reason', '')}{' [pinned]' if r.get('pinned') else ''}",
                            )
                            for r in v["snapshots"]
                        ],
                        [Column(), Column(), Column(align="right"), Column(align="right"), Column(wrap=True)],
                    )
                )
            )
        if not v["snapshots"]:
            print("  (none)")
    elif verb == "show":
        print(f"{v['id']}  {v['created_at']}  host={v['host']}  axiom={v['axiom_version']}  "
              f"reason={v['reason']}{'  [pinned]' if v['pinned'] else ''}")
        print(f"  {v['files']} files, {_kb(v['bytes'])}; tiers: {v['tiers']}")
        print(f"  critical files: {', '.join(v['critical'][:12])}{' ...' if len(v['critical']) > 12 else ''}")
        for ex in v["excluded"][:15]:
            n = "dir skipped" if ex.get("files") is None else f"{ex['files']} file(s)"
            print(f"  excluded [{ex['tier']:11}] {ex['pattern']:32} {n:14} {ex['reason']}")
        if v["secret_metadata"]:
            print(f"  credentials left out (names only): {', '.join(v['secret_metadata'])}")
        db = v["db"]
        print(f"  database: {'DUMPED ' + db['dump']['relpath'] if db.get('dump') else 'NOT covered -- ' + db.get('how', '')}")
        print(f"  chain_hash {v['chain_hash'][:16]}  ledger_head {v['ledger_head'][:16]}")
    elif verb == "diff":
        for c in v["changes"]:
            print(f"  {c['action']:8} {c['path']}")
        print(f"  {len(v['changes'])} change(s), {v['unchanged']} identical")
        if v.get("kept"):
            print(f"  left alone, never rolled back: {', '.join(v['kept'])}")
    elif verb == "verify":
        print(f"  checked {v['checked']} ({'deep' if v['deep'] else 'quick'}); ledger "
              f"{'ok' if v['ledger_ok'] else 'BROKEN'} ({v['ledger_entries']} entries)")
    elif verb == "restore":
        for c in v.get("changes", []):
            print(f"  {c['action']:8} {c['path']}")
        if v.get("pre_restore_snapshot"):
            print(f"  pre-restore snapshot: {v['pre_restore_snapshot']}")
        for w in v.get("warnings", []):
            print(f"WARNING: {w}")
        for path, aside in (v.get("set_aside") or {}).items():
            print(f"  kept previous {path} as {aside}")
        if v.get("dry_run"):
            print("DRY RUN: nothing was written.")
    elif verb == "prune":
        for d in v["delete"]:
            print(f"  {'delete' if v['applied'] else 'would delete'} {d['id']}: {', '.join(d['reasons'])}")
        for d in v["keep"][:50]:
            print(f"  keep   {d['id']}: {', '.join(d['reasons'])}")
        for w in v["warnings"]:
            print(f"WARNING: {w}")
        if not v["applied"]:
            print("DRY RUN: nothing was deleted.")
    for line in v.get("coverage", []) if verb in ("snapshot", "ls", "show") else []:
        print(line)
    for e in res.errors:
        print(f"ERROR: {e}")
    nxt = v.get("next_steps", [])
    if nxt:
        print("next:")
        for n in nxt:
            print(f"  {n}")


def _emergency(args) -> int:
    from axiom.infra import state_snapshots as ss
    from axiom.infra.paths import get_user_state_dir

    state_dir = get_user_state_dir()
    root = ss.Store(__import__("pathlib").Path(args.to) if args.to else ss.default_snapshot_root(state_dir)).root
    script = ss.emergency_script_path(root)
    if script is None:
        print("ERROR: the emergency restore script was not found. It lives at scripts/axi-restore.py in the "
              f"source tree and is copied to {root / ss.EMERGENCY_SCRIPT} on every snapshot.")
        return 1
    cmd = [sys.executable, "-I", str(script), "state", "restore", args.id, "--state", str(state_dir), "--snapshots", str(root)]
    for o in args.only:
        cmd += ["--only", o]
    if args.apply:
        cmd.append("--apply")
    if args.force:
        cmd.append("--force")
    return subprocess.call(cmd)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.verb:
        parser.print_help()
        print(f"\nnext: {_brand_cli()} state ls    |    {_brand_cli()} state snapshot")
        return 0
    verb = args.verb
    if verb == "restore" and args.emergency:
        return _emergency(args)
    params: dict[str, Any] = {"to": getattr(args, "to", None)}
    if verb == "snapshot":
        params.update(reason=args.reason, force=args.force, no_prune=args.no_prune, with_db=args.with_db,
                      db_url=args.db_url, yes_this_database=args.yes_this_database)
    elif verb in ("show", "diff", "pin", "unpin"):
        params["id"] = args.id
        if verb == "diff":
            params["only"] = args.only
    elif verb == "verify":
        params.update(id=args.id, quick=args.quick)
    elif verb == "restore":
        params.update(id=args.id, only=args.only, apply=args.apply, force=args.force)
    elif verb == "prune":
        params.update(apply=args.apply, harder=args.harder)
    res = _run(verb, params)
    if args.json:
        print(json.dumps({"ok": res.ok, "value": res.value, "errors": res.errors}, indent=2, default=str))
    else:
        _render(verb, res)
    return res.exit_code


if __name__ == "__main__":
    sys.exit(main())
