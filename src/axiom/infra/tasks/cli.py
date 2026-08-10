# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""``axi tasks`` CLI — list / show / spawn / cancel / clear / tail.

Wired into ``axiom.axiom_cli.SUBCOMMANDS`` as the ``tasks`` noun.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .runner import TaskRunner
from .store import Task, TaskStore


def _principal_default() -> str:
    """Best-effort principal lookup. Falls back to ``@local:host`` if no
    federation identity is registered yet so single-machine users aren't
    blocked from spawning tasks before running ``axi federation init``.
    """
    try:
        from axiom.vega.federation.identity import load_identity

        ident = load_identity()
        if ident and ident.display_name:
            disp = ident.display_name
            return disp if disp.startswith("@") else f"@{disp}"
    except Exception:
        pass
    import socket

    return f"@local:{socket.gethostname().split('.')[0]}"


def _format_task_row(t: Task) -> str:
    started = (t.started_at or t.created_at or "")[:19]
    duration = ""
    if t.started_at and t.ended_at:
        try:
            from datetime import datetime

            d = (datetime.fromisoformat(t.ended_at) - datetime.fromisoformat(t.started_at))
            duration = f" {d.total_seconds():.1f}s"
        except Exception:
            pass
    return (
        f"  {t.task_id}  {t.status:9s}  {started}{duration}  "
        f"{t.spawner_principal:24s}  {t.name}"
    )


def _cmd_list(args, store: TaskStore) -> int:
    tasks = store.list(status=args.status)
    if not tasks:
        print("  No tasks." if not args.status else f"  No {args.status} tasks.")
        return 0
    print(f"\n  Tasks ({len(tasks)}):")
    print(f"  {'id':12s}  {'status':9s}  {'started':19s}  {'principal':24s}  name")
    for t in tasks:
        print(_format_task_row(t))
    print()
    return 0


def _cmd_show(args, store: TaskStore, runner: TaskRunner) -> int:
    t = runner.status(args.task_id)
    if t is None:
        print(f"  Unknown task: {args.task_id}")
        return 1
    print(f"\n  Task {t.task_id}")
    print(f"    name:       {t.name}")
    print(f"    status:     {t.status}")
    print(f"    principal:  {t.spawner_principal}")
    print(f"    pid:        {t.pid}")
    print(f"    command:    {' '.join(t.command)}")
    print(f"    cwd:        {t.cwd}")
    print(f"    started:    {t.started_at or '—'}")
    print(f"    ended:      {t.ended_at or '—'}")
    print(f"    exit_code:  {t.exit_code if t.exit_code is not None else '—'}")
    print(f"    output:     {t.output_path}")
    if args.tail:
        print(f"\n  Last {args.tail} lines:")
        out = runner.tail(t.task_id, n=args.tail)
        for line in out.splitlines():
            print(f"    {line}")
    print()
    return 0


def _cmd_spawn(args, store: TaskStore, runner: TaskRunner) -> int:
    if not args.command:
        print("  Usage: axi tasks spawn <name> -- <command...>")
        return 2
    cwd = Path(args.cwd or Path.cwd()).resolve()
    principal = args.principal or _principal_default()
    t = runner.spawn(
        name=args.name,
        command=args.command,
        cwd=cwd,
        principal=principal,
    )
    print(f"  Spawned task {t.task_id} (pid={t.pid}, principal={principal})")
    print(f"  Tail with: axi tasks tail {t.task_id}")
    return 0


def _cmd_cancel(args, runner: TaskRunner) -> int:
    try:
        t = runner.cancel(args.task_id)
    except KeyError:
        print(f"  Unknown task: {args.task_id}")
        return 1
    print(f"  Task {t.task_id} -> {t.status}")
    return 0


def _cmd_tail(args, runner: TaskRunner) -> int:
    try:
        out = runner.tail(args.task_id, n=args.n)
    except KeyError:
        print(f"  Unknown task: {args.task_id}")
        return 1
    print(out)
    return 0


def _cmd_clear(args, store: TaskStore) -> int:
    n = store.clear()
    print(f"  Cleared {n} terminal task(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="axi tasks",
        description="Persistent, federation-aware background tasks.",
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    p_list = sub.add_parser("list", help="List tasks (newest first)")
    p_list.add_argument(
        "--status",
        choices=["pending", "running", "done", "failed", "cancelled"],
        help="Filter by status.",
    )

    p_show = sub.add_parser("show", help="Show task details + tail output")
    p_show.add_argument("task_id")
    p_show.add_argument("--tail", type=int, default=20, help="Lines to tail.")

    p_spawn = sub.add_parser("spawn", help="Spawn a new background task")
    p_spawn.add_argument("--cwd", help="Working directory (default: cwd).")
    p_spawn.add_argument("--principal", help="Override spawner principal.")
    p_spawn.add_argument("name", help="Short human label.")
    p_spawn.add_argument(
        "command",
        nargs="+",
        help="Command + args. Use `--` before command if it starts with a dash.",
    )

    p_cancel = sub.add_parser("cancel", help="Cancel a running task (SIGTERM)")
    p_cancel.add_argument("task_id")

    p_tail = sub.add_parser("tail", help="Tail a task's output")
    p_tail.add_argument("task_id")
    p_tail.add_argument("-n", type=int, default=50, help="Lines.")

    sub.add_parser("clear", help="Remove done/failed/cancelled tasks")

    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    store = TaskStore()
    runner = TaskRunner(store)

    if args.verb == "list":
        return _cmd_list(args, store)
    if args.verb == "show":
        return _cmd_show(args, store, runner)
    if args.verb == "spawn":
        return _cmd_spawn(args, store, runner)
    if args.verb == "cancel":
        return _cmd_cancel(args, runner)
    if args.verb == "tail":
        return _cmd_tail(args, runner)
    if args.verb == "clear":
        return _cmd_clear(args, store)
    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
