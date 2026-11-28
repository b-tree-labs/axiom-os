# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Can this run merge, and why: the first line of every run summary (#2238).

``mode`` is the report posture and never gated landing; two pull requests merged under
``mode = "warn"`` on 2026-11-21 because nothing said so. ``land`` is the explicit switch.
A run can merge only when none of these stops it, and the line names each one in the same
order, whichever stops it: the run's event, the kill switch, the release lock, ``land``,
and ``raci.auto_allowed``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from axiom.extensions.builtins.release.lander.config import LanderConfig


@dataclass(frozen=True)
class Posture:
    can_merge: bool
    line: str

    def to_dict(self) -> dict:
        return {"can_merge": self.can_merge, "line": self.line}


def landing_posture(
    config: LanderConfig,
    *,
    environ: dict[str, str] | None = None,
    lock: str = "",
    event: str = "",
) -> Posture:
    """The posture of one run. ``lock`` is the release lock's description, empty when none."""
    env = os.environ if environ is None else environ
    killed = env.get(config.kill_switch_env, "") == "1"
    stops: list[str] = []
    if event == "pull_request":
        stops.append("a pull-request run only reports; landing runs from main")
    if killed:
        stops.append(f"kill switch {config.kill_switch_env}=1")
    if lock:
        stops.append(f"release lock held ({lock})")
    if not config.land:
        stops.append("land = false in the config")
    if "merge" not in config.auto_allowed and "enqueue" not in config.auto_allowed:
        stops.append("raci.auto_allowed does not allow merge, so a merge needs an approved proposal")
    facts = (
        f"mode = {config.mode}, land = {str(config.land).lower()}, "
        f"kill switch {config.kill_switch_env} {'on' if killed else 'off'}, "
        f"{'release lock ' + lock if lock else 'no release lock'}"
    )
    if stops:
        return Posture(False, f"This run cannot merge: {'; '.join(stops)} ({facts}).")
    return Posture(True, f"This run can merge: {facts}. Mode is the report posture only.")
