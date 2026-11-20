# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``flags``: feature flags at the command line (ADR-056, ADR-210, ADR-254 amended 2026-11-12).

    flags [list] [--prefix P] [--user U] [--account A]   every flag, its value and what decided it
    flags resolve [KEY] [--node N] [--account A] [--user U] [--role R ...]
    flags set KEY on|off|soon [--scope node|account|user] [--id ID] [--reason R] [--expires T]
    flags clear KEY [--scope ...] [--id ID]
    flags explain KEY [--user U] [--account A] [--node N] [--role R ...]  why a flag has its value
    flags kill KEY --reason R [--env E] | kill KEY --release
    flags cohort list | show KEY | set KEY ... [--apply] | delete KEY [--apply]
    flags cohort import KEY --file F [--mode add|replace] [--kind principal|account] [--apply]
    flags cohort refresh KEY             take a following cohort's members from its source now
    flags rollout show KEY [--env E] | set KEY --rules JSON [--bucket-by B] [--env E] [--apply]
    flags stage show KEY | set KEY --plan JSON [--stage N] [--apply] | halt KEY --reason R [--rollback]
    flags approve ID                     approve a change held for a second person
    flags changes [--status S]           changes waiting on approval or on their time
    flags cancel ID --reason R           cancel a waiting change
    flags tick [--now T]                 one scheduler pass (the scheduler runs it every minute)
    flags stale [--env E] [--root DIR ...]  flags that are work to remove, by owner (LOOP-F11)

Any cohort, rollout or stage-plan ``set`` (and ``cohort import``) takes ``--at TIME
--timezone ZONE`` to apply later; it is previewed, and with ``--apply`` approved, exactly
like an immediate change (LOOP-F19). A cohort or rollout ``set``/``delete`` previews what
would change and who it touches, and writes only with ``--apply`` (the person at the
terminal is the approval; every other surface needs an approved plan). ``kill`` applies
at once.

What a node runs (extensions, functions, its role and policies) is ``features``. Both
nouns read and write the one registry. Thin argparse over the ``flags.*`` skills: every
verb goes through ``invoke_capability``, so a write is gated, audited and measured
exactly as it is from MCP or the web.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from axiom.infra.cli_format import Column, table


def _cli_name() -> str:
    try:
        from axiom.infra.branding import get_branding

        return (get_branding().cli_name or "axi").strip()
    except Exception:  # noqa: BLE001
        return "axi"


def _registry_context():
    """Every flag verb plus this package's own (the lifecycle verbs, LOOP-F11)."""
    import logging

    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillContext, SkillRegistry

    from .skills import bind

    registry = SkillRegistry()
    bind(registry)
    return SkillContext(registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.flags"))


def run_capability(capability: str, params: dict[str, Any]):
    from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability

    ctx = _registry_context()
    return invoke_capability(ctx.registry, capability, params, ctx, surface=CLI_SURFACE)


def build_parser() -> argparse.ArgumentParser:
    cli = _cli_name()
    p = argparse.ArgumentParser(
        prog=f"{cli} flags",
        description=(
            "feature flags: list, resolve, set, explain, the kill switch, cohorts, rollouts, stage plans, "
            f"schedules and approvals (what this node runs is `{cli} features`)"
        ),
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="verb")

    ls = sub.add_parser("list", help="every flag, its value and what decided it (the default verb)")
    ls.add_argument("--prefix", default="", help="only keys with this prefix")
    ls.add_argument("--user", default="", help="resolve the values for this user")
    ls.add_argument("--account", default="", help="resolve the values for this account")

    st = sub.add_parser("set", help="override a flag at node, account or user scope")
    st.add_argument("key")
    st.add_argument("value", help="on | off | soon | clear")
    st.add_argument("--reason", default="", help="why (required for a flag protected in this environment)")
    st.add_argument("--expires", default="", help="ISO-8601 time the override stops applying")
    cl = sub.add_parser("clear", help="remove an override: back to the layer below")
    cl.add_argument("key")
    for sp in (st, cl):
        sp.add_argument("--scope", default="node", choices=("node", "account", "user"))
        sp.add_argument("--id", dest="scope_id", default="", help="the node, account or user id")

    rs = sub.add_parser("resolve", help="what a node, account and user resolve for every flag")
    rs.add_argument("key", nargs="?", default="")
    rs.add_argument("--node", default="")
    rs.add_argument("--account", default="")
    rs.add_argument("--user", default="")
    rs.add_argument("--role", dest="roles", action="append", default=None, help="a role of the caller")

    ex = sub.add_parser("explain", help="why a flag has its value for one subject")
    ex.add_argument("key")
    for name in ("--node", "--account", "--user", "--locale", "--principal-kind", "--timezone", "--country",
                 "--region", "--at"):
        ex.add_argument(name, default="")
    ex.add_argument("--role", dest="roles", action="append", default=None, help="a role of the caller")

    co = sub.add_parser("cohort", help="named, stored sets a rollout targets: list, show, set, delete")
    co_sub = co.add_subparsers(dest="cohort_verb")
    co_sub.add_parser("list", help="the stored cohorts, rollouts and kill switches")
    cs = co_sub.add_parser("show", help="one cohort")
    cs.add_argument("key")
    cset = co_sub.add_parser("set", help="create or replace a cohort (previews unless --apply)")
    cset.add_argument("key")
    cset.add_argument("--name", default="")
    cset.add_argument("--description", default="")
    cset.add_argument("--rule", dest="rules", default="", help="the rule as JSON")
    cset.add_argument("--principal", dest="principals", action="append", default=[])
    cset.add_argument("--account", dest="accounts", action="append", default=[])
    cset.add_argument("--source-group", dest="source_group", default="",
                      help="follow this directory group (its members replace the list)")
    cset.add_argument("--refresh-every", dest="refresh_every", default="", help="with --source-group: 30m, 1h, 1d")
    cdel = co_sub.add_parser("delete", help="delete a cohort nothing uses (previews unless --apply)")
    cdel.add_argument("key")
    cimp = co_sub.add_parser("import", help="import a large member list from a file (previews unless --apply)")
    cimp.add_argument("key")
    cimp.add_argument("--file", required=True, help="one member per line, a CSV (first column) or a JSON list")
    cimp.add_argument("--kind", default="principal", choices=("principal", "account"))
    cimp.add_argument("--mode", default="replace", choices=("replace", "add"))
    cref = co_sub.add_parser("refresh", help="take a following cohort's members from its source now")
    cref.add_argument("key")
    for sp in (cset, cdel, cimp):
        sp.add_argument("--reason", default="")
        sp.add_argument("--apply", action="store_true", help="write it; without this, preview only")
        sp.add_argument("--at", default="", help="apply at this time instead of now (ISO-8601)")
        sp.add_argument("--timezone", default="", help="the IANA zone --at is in")

    sl = sub.add_parser("stale", help="flags that are work to remove: fully on past review, or off and unreferenced")
    sl.add_argument("--env", dest="environment", default="", help="the environment (default: this one)")
    sl.add_argument("--today", default="", help="judge as of this date (YYYY-MM-DD)")
    sl.add_argument("--root", dest="roots", action="append", default=[],
                    help="a directory the reference search reads (default: the platform and its extensions)")

    ro_p = sub.add_parser("rollout", help="a flag's ordered rollout rules in one environment: show, set")
    ro_sub = ro_p.add_subparsers(dest="rollout_verb")
    rshow = ro_sub.add_parser("show", help="one flag's rollout and kill switch")
    rshow.add_argument("key")
    rset = ro_sub.add_parser("set", help="replace a flag's rules (previews unless --apply)")
    rset.add_argument("key")
    rset.add_argument("--rules", required=True, help="the ordered rules as JSON; [] for none")
    rset.add_argument("--bucket-by", dest="bucket_by", default="",
                      help="the unit a percentage moves: principal, account, any attribute, or cohort:a,b")
    rset.add_argument("--reason", default="")
    rset.add_argument("--apply", action="store_true", help="write it; without this, preview only")
    rset.add_argument("--at", default="", help="apply at this time instead of now (ISO-8601)")
    rset.add_argument("--timezone", default="", help="the IANA zone --at is in")
    for sp in (rshow, rset):
        sp.add_argument("--env", dest="environment", default="")

    st_p = sub.add_parser("stage", help="a flag's progressive rollout: show, set, halt")
    st_sub = st_p.add_subparsers(dest="stage_verb")
    sshow = st_sub.add_parser("show", help="one flag's stage plan and where it stands")
    sshow.add_argument("key")
    sset = st_sub.add_parser("set", help="start, replace or resume a stage plan (previews unless --apply)")
    sset.add_argument("key")
    sset.add_argument("--plan", required=True, help="the plan as JSON")
    sset.add_argument("--stage", type=int, default=0, help="the stage to start at (resume a halted plan)")
    sset.add_argument("--reason", default="")
    sset.add_argument("--apply", action="store_true", help="write it; without this, preview only")
    sset.add_argument("--at", default="", help="start at this time instead of now (ISO-8601)")
    sset.add_argument("--timezone", default="", help="the IANA zone --at is in")
    shalt = st_sub.add_parser("halt", help="stop a stage plan where it stands, at once")
    shalt.add_argument("key")
    shalt.add_argument("--reason", default="")
    shalt.add_argument("--rollback", action="store_true", help="roll back: the plan serves nobody")
    for sp in (sshow, sset, shalt):
        sp.add_argument("--env", dest="environment", default="")

    ap_p = sub.add_parser("approve", help="approve a change held for a second person")
    ap_p.add_argument("id")
    ap_p.add_argument("--reason", default="")
    ch_p = sub.add_parser("changes", help="changes waiting on approval or on their time")
    ch_p.add_argument("--status", default="")
    ca_p = sub.add_parser("cancel", help="cancel a change that waits")
    ca_p.add_argument("id")
    ca_p.add_argument("--reason", default="")
    tk_p = sub.add_parser("tick", help="one scheduler pass: due changes, stage plans, followed cohorts")
    tk_p.add_argument("--now", default="")

    kl = sub.add_parser("kill", help="pull a flag's kill switch: off for everyone, at once")
    kl.add_argument("key")
    kl.add_argument("--reason", default="")
    kl.add_argument("--env", dest="environment", default="")
    kl.add_argument("--release", action="store_true", help="release the kill switch")
    return p


#: Kept for callers of the old name.
get_parser = build_parser


# ------------------------------------------------------------------ printers


def waiting_text(extension: str) -> str:
    from axiom.extensions.builtins.features.resolver import waiting_text as _text

    return _text(extension)


def print_flag_table(rows: list[dict[str, Any]], *, environment: str = "") -> list[str]:
    """The flag rows as a table: value, where it came from, default, owner."""
    lines = [f"environment: {environment}"] if environment else []
    if not rows:
        lines.append("(no flags are declared)")
        return lines
    cells = []
    for r in rows:
        d = r.get("details") or {}
        notes = []
        if d.get("protected"):
            notes.append("protected")
        if d.get("waiting_on"):
            note = waiting_text(d["waiting_on"])
            if d.get("staged_value") is not None:
                note += f" (then {d['staged_value']} from {d.get('staged_source')})"
            notes.append(note)
        elif d.get("owner_off"):
            notes.append("extension off")
        cells.append([r["name"], r["value"], r["source"], str(d.get("default") or ""), r["owner"],
                      ", ".join(notes), r.get("description") or ""])
        for o in d.get("overrides") or []:
            cells.append([f"  {o['scope']}:{o['scope_id']}", o["value"], o["updated_by"], "", "", "",
                          o.get("updated_at") or ""])
    cols = [Column("flag"), Column("value"), Column("from"), Column("default"), Column("owner"),
            Column(""), Column("", wrap=True)]
    lines.extend(table(cells, cols, headers=True))
    return lines


def print_flag_list(value: dict[str, Any]) -> list[str]:
    """``flags.list``: the resolved rows when there are, else the declarations alone."""
    lines = [f"note: {value['problem']}"] if value.get("problem") else []
    if value.get("rows"):
        return lines + print_flag_table(value["rows"], environment=value["environment"])
    lines.append(f"environment: {value['environment']}")
    if not value["flags"]:
        lines.append("(no flags are declared)")
        return lines
    rows = []
    for row in value["flags"]:
        rows.append([row["key"], row["default"], row["owner"],
                     "protected" if row["protected"] else "", row["description"]])
        for o in row["overrides"]:
            rows.append([f"  {o['scope']}:{o['scope_id']}", o["value"], o["updated_by"],
                         "", o["updated_at"] or ""])
    cols = [Column("flag"), Column("value"), Column("owner / by"), Column(""), Column("", wrap=True)]
    lines.extend(table(rows, cols, headers=True))
    return lines


def print_resolve(value: dict[str, Any]) -> list[str]:
    who = ", ".join(f"{k}={v}" for k, v in value["for"].items() if v)
    waiting = value.get("waiting_on") or {}
    staged = value.get("staged") or {}
    rows = []
    for key, val in value["flags"].items():
        note = ""
        if key in waiting:
            note = waiting_text(waiting[key])
            if key in staged:
                note += f" (then {staged[key]['value']} from {staged[key]['source']})"
        rows.append([key, val, value["sources"][key], note])
    return [f"environment: {value['environment']}  ({who})",
            *table(rows, [Column("flag"), Column("value"), Column("from"), Column("", wrap=True)], headers=True)]


def print_explain(v: dict[str, Any]) -> list[str]:
    subject = ", ".join(f"{k}={val}" for k, val in (v.get("subject") or {}).items() if val)
    d = v.get("detail") or {}
    lines = [f"{v['flag']} = {v['value']}  (environment {v['environment']}; {subject or 'no subject'})"]
    how = f"decided by: {v['decided_by']}"
    if v["decided_by"] == "rollout":
        target = d.get("cohort") and f"cohort {d['cohort']}" or (d.get("match") and "a match") or "everyone"
        how += f", rule {d.get('rule_index')} ({target})"
        if d.get("percentage") is not None:
            how += f", {d['percentage']}% with bucket {d.get('bucket'):.2f}"
    for k in ("reason", "expires_at", "set_by", "variable", "extension"):
        if d.get(k):
            how += f"; {k.replace('_', ' ')}: {d[k]}"
    lines.append(how)
    if v.get("bucket") is not None:
        lines.append(f"bucket: {v['bucket']:.2f} (by {v['bucket_by']})")
    for e in v.get("evaluated") or []:
        if "cohorts_matched" in e:
            got = ", ".join(f"{k}={'unknown' if m is None else m}" for k, m in e["cohorts_matched"].items())
            lines.append(f"  rule {e['rule_index']}: member={e.get('member')}" + (f" ({got})" if got else ""))
    for i in v.get("ignored") or []:
        lines.append(f"ignored: {i['step']} ({i['status']})" + (f", value {i['value']}" if i.get("value") else ""))
    if v.get("unknown_attributes"):
        lines.append(f"unknown attributes: {', '.join(v['unknown_attributes'])} (unknown never admits)")
    if v.get("kill_switch"):
        lines.append("kill switch: engaged")
    if not (v.get("cohorts") or {}).get("available", True):
        lines.append(f"note: {v['cohorts']['why']}")
    return lines


def print_preview(v: dict[str, Any], *, verb: str) -> list[str]:
    cli = _cli_name()
    lines = [f"preview (nothing written): {verb} {v.get('key')}"]
    lines.append(f"  before: {json.dumps(v.get('before'), default=str)}")
    lines.append(f"  after:  {json.dumps(v.get('after'), default=str)}")
    if "members" in v:
        m = v["members"]
        lines.append(f"  members: {m['count']} of {v['population']['size']} known" + (
            f" ({', '.join(m['sample'])})" if m["sample"] else ""))
    if "changes" in v:
        c = v["changes"]
        lines.append(f"  changes: {c['count']} of {v['population']['size']} known" + (
            f" ({', '.join(c['sample'])})" if c["sample"] else ""))
    if "added" in v:
        lines.append(f"  members: {v['after'].get('size')} listed (+{v['added']}, -{v['removed']})")
    for st in v.get("stages") or []:
        lines.append(f"  stage {st['index'] + 1}: {st['label']}, soak {st['soak_s'] // 60} min: admits "
                     f"{st['admitted']} of {v['population']['size']} known")
    when = v.get("scheduled_for")
    if when:
        lines.append(f"  scheduled for: {when['local']} {when['timezone'] or 'UTC'} ({when['run_at']})")
    if v.get("used_by"):
        lines.append(f"  used by: {', '.join(v['used_by'])}")
    lines.append(f"Write it: run the same `{cli} flags ...` with --apply (and a --reason).")
    return lines


def print_stale(v: dict[str, Any]) -> list[str]:
    lines = [f"{len(v['stale'])} of {v['declared']} flag(s) are work to remove in {v['environment']} "
             f"(as of {v['today']})"]
    for owner, keys in sorted(v["by_owner"].items()):
        lines.append(f"{owner}:")
        for item in (i for i in v["stale"] if i["key"] in keys):
            lines.append(f"  {item['key']}  {item['case']}: {item['why']}")
    if v["unowned"]:
        lines.append(f"unowned: {', '.join(v['unowned'])} (declare an owner in the manifest: @maintainers)")
    if v.get("note"):
        lines.append(f"note: {v['note']}")
    if not str(v.get("record", "")).startswith("read"):
        lines.append(f"note: the record was {v['record']}")
    return lines


# ------------------------------------------------------------------ dispatch


def _call(args: argparse.Namespace) -> tuple[str, dict[str, Any], str]:
    """The capability, its parameters and the printer for one verb."""
    verb = args.verb or "list"
    if verb == "list":
        return "flags.list", {"prefix": getattr(args, "prefix", ""), "user": getattr(args, "user", ""),
                              "account": getattr(args, "account", "")}, "list"
    if verb in ("set", "clear"):
        params = {"key": args.key, "value": args.value if verb == "set" else "clear",
                  "scope": args.scope, "scope_id": args.scope_id}
        if verb == "set":
            params["reason"] = args.reason
            params["expires_at"] = args.expires
        return "flags.set", params, "actions"
    if verb == "resolve":
        return "flags.resolve", {"key": args.key, "node": args.node, "account": args.account, "user": args.user,
                                 "roles": args.roles}, "resolve"
    if verb == "explain":
        return "flags.explain", {
            "key": args.key, "node": args.node, "account": args.account, "user": args.user,
            "roles": args.roles, "locale": args.locale, "principal_kind": args.principal_kind,
            "timezone": args.timezone, "country": args.country, "region": args.region, "at": args.at,
        }, "explain"
    if verb == "kill":
        return "flags.kill", {"key": args.key, "reason": args.reason, "environment": args.environment,
                              "engaged": not args.release}, "actions"
    if verb == "cohort":
        cv = args.cohort_verb or "list"
        if cv in ("list", "show"):
            return "flags.cohorts", {}, f"cohort-{cv}"
        when = {k: v for k, v in (("at", getattr(args, "at", "")), ("timezone", getattr(args, "timezone", ""))) if v}
        if cv == "set":
            params = {"key": args.key, "name": args.name, "description": args.description,
                      "rules": args.rules, "principals": args.principals, "accounts": args.accounts, **when}
            if args.source_group:
                params["source"] = {"kind": "directory", "group": args.source_group}
                if args.refresh_every:
                    params["refresh_every"] = args.refresh_every
            if not args.apply:
                return "flags.cohort_preview", params, "preview"
            return "flags.cohort_set", {**params, "reason": args.reason}, "actions"
        if cv == "delete":
            if not args.apply:
                return "flags.cohorts", {}, "cohort-delete-preview"
            return "flags.cohort_delete", {"key": args.key, "reason": args.reason, **when}, "actions"
        if cv == "import":
            params = {"key": args.key, "file": args.file, "kind": args.kind, "mode": args.mode, **when}
            if not args.apply:
                return "flags.cohort_import_preview", params, "preview"
            return "flags.cohort_import", {**params, "reason": args.reason}, "actions"
        return "flags.cohort_refresh", {"key": args.key}, "actions"
    if verb == "rollout":
        rv = args.rollout_verb or "show"
        if rv == "show":
            return "flags.cohorts", {"environment": args.environment}, "rollout-show"
        params = {"key": args.key, "rules": args.rules, "environment": args.environment,
                  "bucket_by": args.bucket_by}
        params.update({k: v for k, v in (("at", args.at), ("timezone", args.timezone)) if v})
        if not args.apply:
            return "flags.rollout_preview", params, "preview"
        return "flags.rollout_set", {**params, "reason": args.reason}, "actions"
    if verb == "stage":
        sv = args.stage_verb or "show"
        if sv == "show":
            return "flags.cohorts", {"environment": args.environment}, "stage-show"
        if sv == "halt":
            return "flags.stage_plan_halt", {"key": args.key, "reason": args.reason,
                                             "environment": args.environment,
                                             "mode": "rollback" if args.rollback else "halt"}, "actions"
        params = {"key": args.key, "plan": args.plan, "environment": args.environment, "stage": args.stage}
        params.update({k: v for k, v in (("at", args.at), ("timezone", args.timezone)) if v})
        if not args.apply:
            return "flags.stage_plan_preview", params, "preview"
        return "flags.stage_plan_set", {**params, "reason": args.reason}, "actions"
    if verb == "approve":
        return "flags.approve", {"id": args.id, "reason": args.reason}, "actions"
    if verb == "changes":
        return "flags.changes", {"status": args.status}, "changes"
    if verb == "cancel":
        return "flags.change_cancel", {"id": args.id, "reason": args.reason}, "actions"
    if verb == "stale":
        return "flags.stale", {"environment": args.environment, "today": args.today, "roots": args.roots}, "stale"
    return "flags.rollout_tick", {"now": args.now} if args.now else {}, "tick"


def _print(printer: str, value: dict[str, Any], args: argparse.Namespace) -> None:
    if printer == "list":
        print("\n".join(print_flag_list(value)))
    elif printer == "resolve":
        print("\n".join(print_resolve(value)))
        if value.get("targeting"):
            print(f"note: {value['targeting']}")
    elif printer == "explain":
        print("\n".join(print_explain(value)))
    elif printer == "preview":
        print("\n".join(print_preview(value, verb=f"{args.verb} set")))
    elif printer == "cohort-list":
        if value.get("targeting"):
            print(f"note: {value['targeting']}")
        for c in value["cohorts"]:
            used = f"  used by {', '.join(c['used_by'])}" if c["used_by"] else ""
            print(f"{c['key']}: {len(c['principals'])} principal(s), {len(c['accounts'])} account(s)"
                  f"{', a rule' if c['rules'] else ''}{used}")
        for r in value["rollouts"]:
            print(f"rollout {r['key']} ({r['environment']}): {len(r['rules'] or [])} rule(s)"
                  f"{'; KILL SWITCH ENGAGED' if r['kill_switch'] else ''}")
    elif printer == "cohort-show":
        hit = [c for c in value["cohorts"] if c["key"] == args.key]
        print(json.dumps(hit[0] if hit else None, indent=2, default=str))
    elif printer == "cohort-delete-preview":
        hit = [c for c in value["cohorts"] if c["key"] == args.key]
        print(f"preview (nothing written): cohort delete {args.key}")
        print(f"  before: {json.dumps(hit[0] if hit else None, default=str)}")
        if hit and hit[0]["used_by"]:
            print(f"  used by: {', '.join(hit[0]['used_by'])} (a cohort in use cannot be deleted)")
        print("Write it: run the same command with --apply (and a --reason).")
    elif printer == "rollout-show":
        hit = [r for r in value["rollouts"] if r["key"] == args.key]
        print(json.dumps(hit[0] if hit else None, indent=2, default=str))
    elif printer == "stage-show":
        hit = [p for p in value.get("stage_plans") or [] if p["key"] == args.key]
        print(json.dumps(hit[0] if hit else None, indent=2, default=str))
    elif printer == "changes":
        rows = [(c["id"], c["status"], c["subject"], c["author"],
                 f"{c['run_at']} ({c['timezone']})" if c.get("run_at") else "",
                 ", ".join(a["by"] for a in c["approvals"])) for c in value["changes"]]
        cols = [Column("id"), Column("status"), Column("change", wrap=True), Column("by"), Column("when"),
                Column("approved by", wrap=True)]
        print("\n".join(table(rows, cols, headers=True)) if rows else "no waiting or past changes")
    elif printer == "stale":
        print("\n".join(print_stale(value)))
    elif printer == "tick":
        print(json.dumps(value, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    capability, params, printer = _call(args)
    result = run_capability(capability, params)
    if not result.ok:
        print("\n".join(result.errors), file=sys.stderr)
        return result.exit_code or 1
    if args.json:
        print(json.dumps(result.value, indent=2, default=str))
        return 0
    for action in result.actions_taken:
        print(f"• {action}")
    if printer != "actions":
        _print(printer, result.value, args)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
