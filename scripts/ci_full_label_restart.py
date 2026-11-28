#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Does adding or removing the ci:full label need CI restarted? (#2441)

CI's change job reads the label live right after its checkout. A label added before that moment
is already in the run, so a restart is a wasted full run. A label CI applied itself (the Actions
bot, from the change job's path rule) is the same case. Removing a label always restarts, and a
run whose timing cannot be read restarts rather than guesses.

    ci_full_label_restart.py decide --label-event FILE --changes-job FILE
    ci_full_label_restart.py decide --action labeled --actor LOGIN --at TIME --changes-job FILE

Prints ``restart=true|false`` then the reason. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

CI_BOT = "github-actions[bot]"


@dataclass(frozen=True)
class Decision:
    restart: bool
    reason: str


def _time(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_at(changes_job: dict | None) -> datetime | None:
    """When the change job could first have read the label: its checkout finished."""
    for step in (changes_job or {}).get("steps") or []:
        name = str(step.get("name") or "")
        if "actions/checkout" in name and not name.startswith("Post"):
            return _time(step.get("completed_at"))
    return None


def decide(event: dict, changes_job: dict | None) -> Decision:
    if str(event.get("event") or "") != "labeled":
        return Decision(True, "the label was removed; the run must read the current labels")
    if str((event.get("actor") or {}).get("login") or "") == CI_BOT:
        return Decision(False, "CI applied the label itself in the run that already read the paths")
    label_at, read_at = _time(event.get("created_at")), _read_at(changes_job)
    if label_at is None or read_at is None:
        return Decision(True, "when the run read the labels could not be read; restarting to be safe")
    if label_at < read_at:
        return Decision(False, f"the label ({label_at:%H:%M:%S}) was there before the run read the labels ({read_at:%H:%M:%S}); it already read it")
    return Decision(True, f"the label ({label_at:%H:%M:%S}) came after the run read the labels ({read_at:%H:%M:%S})")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["decide"])
    parser.add_argument("--label-event")
    parser.add_argument("--action")
    parser.add_argument("--actor")
    parser.add_argument("--at")
    parser.add_argument("--changes-job")
    args = parser.parse_args(argv[1:])
    if args.label_event:
        event = json.loads(Path(args.label_event).read_text())
    else:
        event = {"event": args.action, "actor": {"login": args.actor}, "created_at": args.at}
    job = None
    if args.changes_job and Path(args.changes_job).is_file():
        try:
            job = json.loads(Path(args.changes_job).read_text() or "null")
        except json.JSONDecodeError:
            job = None
    decision = decide(event, job if isinstance(job, dict) else None)
    print(f"restart={'true' if decision.restart else 'false'}")
    print(decision.reason)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
