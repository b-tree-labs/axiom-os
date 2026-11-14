# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi instrument video``: declare, verify, start, stop and observe video sources (spec-video §CLI).

    axi instrument video add <name> --kind rtsp --url <rtsp://host/path> [--credential-ref <ref>]
                                    [--transport tcp|udp] [--fps-cap N]
    axi instrument video verify <name>
    axi instrument video list [--kinds | --triggers]
    axi instrument video status [<name>]
    axi instrument video events [<name>] [--since <iso>]
    axi instrument video start <name>
    axi instrument video stop <name>
    axi instrument video diagnose <name>
    axi instrument video install       # capture comes back after a reboot
    axi instrument video uninstall
    axi instrument video revoke <name>

Video is instrument kind ``video`` (ADR-256): the ``instrument`` noun hands this
module the rest of the command line and the command the person typed as ``prog``
(``video/instrument.py``). Every verb takes ``--json``. Per ADR-056 each verb is a
thin wrapper: flags become a params dict and go to skill ``video.<verb>`` through
``invoke_capability``; the skills keep their names (ADR-256 D5). A source kind's own
flags (``--url``, ``--transport`` for rtsp) come from its registration, so a new
source kind brings its flags with it. A trigger is attached by configuration and
``connector reload``, so there is no verb for that (VID-015).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from ..naming import command as _command

#: Every verb, in the order ``--help`` lists them: phase 1's, then ``events``.
VERBS = (
    "add",
    "verify",
    "list",
    "status",
    "start",
    "stop",
    "diagnose",
    "install",
    "uninstall",
    "revoke",
    "events",
)

#: Prefix of the argparse destinations that hold a kind's own parameters.
SOURCE_PREFIX = "source__"


def skill_for(verb: str) -> str:
    return f"video.{verb}"


def _cli_name() -> str:
    from axiom.extensions.builtins.video.naming import cli_name

    return cli_name()


def _kind_flags(parser: argparse.ArgumentParser) -> None:
    """``--<param>`` for every parameter any registered kind declares (union, first help wins)."""
    from ..sources import source_kinds

    seen: set[str] = set()
    for kind in source_kinds():
        for param in kind.params:
            if param.name in seen:
                continue
            seen.add(param.name)
            extra: dict[str, Any] = {}
            if param.choices:
                extra["choices"] = list(param.choices)
            parser.add_argument(
                f"--{param.name.replace('_', '-')}",
                dest=f"{SOURCE_PREFIX}{param.name}",
                default=None,
                help=f"({kind.kind}) {param.help}"
                + (f"; default {param.default}" if param.default else ""),
                **extra,
            )


def get_parser(prog: str | None = None) -> argparse.ArgumentParser:
    """The video verbs. ``prog`` is the command the person typed (``acme instrument video``);
    by default ``<cli> instrument video``."""
    from ..sources import source_kinds

    cli = _cli_name()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print the result envelope as JSON")

    parser = argparse.ArgumentParser(
        prog=prog or _command(),
        description=(
            "Video sources: declare, verify, capture and observe them "
            "(instrument kind video; ADR-240, ADR-256)."
        ),
    )
    sub = parser.add_subparsers(dest="verb", metavar="{" + ",".join(VERBS) + "}")

    add = sub.add_parser("add", parents=[common], help="declare a source and verify it")
    add.add_argument("name", help="the source's name (lowercase letters, digits, - and _)")
    add.add_argument(
        "--kind", required=True, choices=[k.kind for k in source_kinds()], help="the source kind"
    )
    add.add_argument(
        "--credential-ref",
        dest="credential_ref",
        default=None,
        help=f"where the login is held, e.g. foreign://video-<name> (store it with `{cli} vault set`)",
    )
    add.add_argument(
        "--fps-cap",
        dest="fps_cap",
        type=int,
        default=None,
        help="decoded frames per second (default 10)",
    )
    _kind_flags(add)

    for verb, helptext in (
        ("verify", "ask a source for one frame and record codec, size, rate, latency"),
        ("start", "start capturing a verified source"),
        ("stop", "stop capturing a source"),
        ("diagnose", "find the first thing that is wrong with a source, and its fix"),
        (
            "revoke",
            "remove a source: stop it, release it, delete its declaration (its login stays)",
        ),
    ):
        p = sub.add_parser(verb, parents=[common], help=helptext)
        p.add_argument("name")

    ls = sub.add_parser(
        "list", parents=[common], help="declared sources, registered source kinds, or triggers"
    )
    what = ls.add_mutually_exclusive_group()
    what.add_argument(
        "--kinds", action="store_true", help="list the registered source kinds instead"
    )
    what.add_argument(
        "--triggers", action="store_true", help="list the triggers that can be attached instead"
    )

    ev = sub.add_parser("events", parents=[common], help="what the triggers saw, from the ledger")
    ev.add_argument("name", nargs="?", default=None, help="optional: one source")
    ev.add_argument("--since", default=None, help="only events at or after this ISO-8601 time")

    st = sub.add_parser("status", parents=[common], help="each source's session and counters")
    st.add_argument("name", nargs="?", default=None)

    sub.add_parser(
        "install",
        parents=[common],
        help="capture started sources again after a reboot (a boot unit)",
    )
    sub.add_parser(
        "uninstall", parents=[common], help="remove the boot unit; started sources stay started"
    )
    return parser


def _params(args: argparse.Namespace) -> dict[str, Any]:
    if args.verb == "add":
        source = {
            key[len(SOURCE_PREFIX) :]: value
            for key, value in vars(args).items()
            if key.startswith(SOURCE_PREFIX) and value is not None
        }
        params: dict[str, Any] = {"name": args.name, "kind": args.kind, "source": source}
        if args.credential_ref:
            params["credential_ref"] = args.credential_ref
        if args.fps_cap is not None:
            params["fps_cap"] = args.fps_cap
        return params
    if args.verb == "list":
        return {"kinds": bool(args.kinds), "triggers": bool(args.triggers)}
    if args.verb == "events":
        return {k: v for k, v in (("name", args.name), ("since", args.since)) if v}
    if args.verb == "status":
        return {"name": args.name} if args.name else {}
    if args.verb in ("install", "uninstall"):
        return {}
    return {"name": args.name}


def _invoke(capability: str, params: dict[str, Any]):
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
    from axiom.infra.skills import SkillContext, SkillRegistry

    from ..skills import bind

    registry = SkillRegistry()
    bind(registry)
    ctx = SkillContext(
        registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.video")
    )
    return invoke_capability(registry, capability, params, ctx, surface=CLI_SURFACE)


# ---- rendering ----------------------------------------------------------------


def _age(seconds: Any) -> str:
    return "never" if seconds is None else f"{float(seconds):.1f} s ago"


def _print_verify(name: str, record: dict[str, Any]) -> None:
    if record.get("ok"):
        r = record.get("report") or {}
        rate = f" at {r['fps']:g} fps" if r.get("fps") else ""
        print(
            f"{name}: {r.get('codec')} {r.get('width')}x{r.get('height')}{rate}; first frame in {r.get('latency_ms'):.0f} ms"
        )
    else:
        print(f"{name}: not verified: {record.get('error')}")
        if record.get("fix"):
            print(f"  fix: {record['fix']}")


def _print_service(service: dict[str, Any]) -> None:
    if not service:
        return
    pids = ", ".join(str(s["pid"]) for s in service.get("running") or [])
    running = f"running (pid {pids})" if pids else "not running"
    if service.get("installed"):
        print(f"boot service: installed ({service.get('provider')}), {running}")
    else:
        print(f"boot service: not installed, {running}")
    if service.get("note"):
        print(f"  note: {service['note']}")


def _print_status(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print(f"(no video sources are declared; `{_command()} add` declares one)")
        return
    for row in rows:
        print(
            f"{row['name']}  {row['kind']}  {row['health']}  {row['state']}{'  (started)' if row['started'] else ''}"
        )
        shown = {k: v for k, v in (row.get("source") or {}).items() if k != "kind"}
        if shown:
            print("  source: " + ", ".join(f"{k}={v}" for k, v in shown.items()))
        if row.get("credential_ref"):
            print(f"  login: {row['credential_ref']}")
        if row.get("session_id"):
            fps = f"{row['fps']:g} fps" if row.get("fps") else "fps unknown"
            print(
                f"  session {row['session_id']} (pid {row.get('pid') or '-'}): {row['frames']} frames, "
                f"{row['drops']} drops, {row['reconnects']} reconnects, {fps}, last frame {_age(row.get('last_frame_age_s'))}"
            )
        verify = row.get("verify") or {}
        if verify:
            report = verify.get("report") or {}
            result = (
                f"{report.get('codec')} {report.get('width')}x{report.get('height')}"
                if verify.get("ok")
                else verify.get("error")
            )
            print(f"  last verify {verify.get('at')}: {result}")
        if row.get("error"):
            print(f"  error: {row['error']}")
        if row.get("fix"):
            print(f"  fix: {row['fix']}")
        if row.get("note"):
            print(f"  note: {row['note']}")
        for copy in row.get("standby") or []:
            print(f"  waiting copy: pid {copy.get('pid')}")
        _print_triggers_of(row)


def _print_table(rows: list[dict[str, Any]], columns: tuple[str, ...]) -> None:
    from axiom.infra.cli_format import Column, table

    for line in table(
        [tuple(str(r.get(c, "")) for c in columns) for r in rows],
        [Column(c) for c in columns],
        headers=True,
    ):
        print(line)


def _n(count: Any, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _print_triggers_of(row: dict[str, Any]) -> None:
    running = {t["name"] for t in row.get("triggers") or []}
    for t in row.get("triggers") or []:
        own = t.get("health") or {}
        parts = [_n(t["frames"], "frame"), _n(t["events"], "event")]
        if own.get("episodes") is not None:
            parts.append(
                _n(own["episodes"], "episode") + (" (one open)" if own.get("open") else "")
            )
        if own.get("last_score") is not None:
            parts.append(f"last score {own['last_score']:.3f}")
        if t.get("dropped"):
            parts.append(f"{t['dropped']} dropped")
        if t.get("errors"):
            parts.append(_n(t["errors"], "error"))
        print(f"  trigger {t.get('trigger') or t['name']}: {t['state']}, " + ", ".join(parts))
        if t.get("state") == "failed" or t.get("errors"):
            if t.get("last_error"):
                print(f"    last error: {t['last_error']}")
            if t.get("fix"):
                print(f"    fix: {t['fix']}")
    idle = [n for n in row.get("attached") or [] if n not in running]
    if idle:
        print(f"  attached: {', '.join(idle)} (they run while the source is captured)")


def _local_time(ts: Any) -> str:
    """An event's time as a person reads it: local, to the second. ``--json`` keeps the ISO."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(ts)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(ts)


def _print_events(rows: list[dict[str, Any]]) -> None:
    """One row per event. The incident key is ``source:trigger@version:episode``, so it names
    the source and the trigger too, and the two events of one episode share it. The score
    is left to ``--json``: the key is what a narrow terminal must not lose."""
    from axiom.infra.cli_format import Column, table, terminal_width

    if not rows:
        print("(no events yet: a trigger attached to a started source writes them here)")
        return
    data = [
        (
            _local_time(r.get("ts", "")),
            r.get("kind", ""),
            r.get("severity", ""),
            r.get("incident_key", ""),
        )
        for r in rows
    ]
    columns = [
        Column("time"),
        Column("kind"),
        Column("severity"),
        Column("incident key", wrap=True),
    ]
    for line in table(data, columns, width=terminal_width(reserve=2), headers=True):
        print(line)


def _print_trigger_list(rows: list[dict[str, Any]]) -> None:
    from axiom.infra.cli_format import Column, table, terminal_width

    if not rows:
        print("(no triggers are registered)")
        return
    data = [
        (
            r["name"],
            r.get("version") or "",
            r["state"],
            r.get("origin") or "",
            r.get("description") if r["state"] == "registered" else r.get("reason") or "",
        )
        for r in rows
    ]
    columns = [
        Column("trigger"),
        Column("version"),
        Column("state"),
        Column("origin", wrap=True),
        Column("what it does", wrap=True),
    ]
    for line in table(data, columns, width=terminal_width(reserve=2), headers=True):
        print(line)


def _print_ladder(ladder: dict[str, Any]) -> None:
    from axiom.infra.cli_format import Column, table, terminal_width

    rows = []
    for step in ladder.get("steps") or []:
        mark = {True: "✓", False: "✗", None: "·"}[step["ok"]]
        rows.append((mark, step["step"], step["detail"]))
        if step["ok"] is False and step.get("fix"):
            rows.append(("", "", f"fix: {step['fix']}"))
    columns = [Column(""), Column("rung"), Column("detail", wrap=True)]
    for line in table(rows, columns, width=terminal_width(reserve=2)):
        print(line)


def _render(verb: str, value: Any, result: Any) -> None:
    value = value or {}
    if verb == "add" and "verify" in value:
        print(f"declared {value['name']} ({value['kind']}) at {value['path']}")
        _print_verify(value["name"], value["verify"])
    elif verb == "verify" and "ok" in value:
        _print_verify(value["name"], value)
    elif verb == "list" and "triggers" in value:
        _print_trigger_list(value["triggers"])
    elif verb == "events" and "events" in value:
        _print_events(value["events"])
    elif verb == "list" and "kinds" in value:
        rows = [
            {**k, "replayable": "replayable" if k["replayable"] else "live"} for k in value["kinds"]
        ]
        _print_table(rows, ("kind", "replayable", "description"))
    elif verb == "list":
        if value.get("sources"):
            _print_table(value["sources"], ("name", "kind", "health", "state"))
        else:
            print(f"(no video sources are declared; `{_command()} add` declares one)")
    elif verb == "status":
        _print_status(value.get("sources") or [])
        _print_service(value.get("service") or {})
    elif verb == "diagnose" and "steps" in value:
        _print_ladder(value)
    else:
        for action in result.actions_taken:
            print(action)


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = get_parser(prog)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not args.verb:
        parser.print_help()
        return 2
    result = _invoke(skill_for(args.verb), _params(args))
    if args.json:
        envelope = {
            "ok": result.ok,
            "value": result.value,
            "errors": result.errors,
            "actions_taken": result.actions_taken,
        }
        if result.unmet:
            envelope["unmet"] = result.unmet
        print(json.dumps(envelope, indent=2, default=str))
        return 0 if result.ok else 1
    if result.ok or result.value:
        _render(args.verb, result.value, result)
    for error in result.errors:
        print(f"error: {error}", file=sys.stderr)
    return 0 if result.ok else 1


cli = main

__all__ = ["SOURCE_PREFIX", "VERBS", "get_parser", "main", "skill_for"]
