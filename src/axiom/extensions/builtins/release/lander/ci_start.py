# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A head CI never ran on: who acts next, and the one start RIVET may spend.

A pull request whose required checks nothing will ever report looks like one
whose checks are running, and it stays that way until somebody notices. The
verdict is ``checks.ci_never_ran``. This file decides what follows it.

RIVET starts CI once per head, by closing and reopening the pull request: the
forge then delivers a ``reopened`` event, which every pull-request workflow
already listens for, with no new commit and no change to the head. A start is
recorded before it is attempted, so a crash, a failure or a second sweep cannot
repeat it. Two records count as spent: this ledger, and a reopen the forge
shows after the head was pushed (a runner that keeps no state between sweeps
has only the second). When the start is spent and the checks are still missing,
the pull request goes to a person.

A draft is reported and left alone: marking it ready is the author's step and
it starts CI. A pull request held for its owner (``lander.owner_hold``) is left
alone the same way until the hold is released. A fork's pull request, one with auto-merge armed (a close would
disarm it), and a repository that has not allowed ``ci_start`` go to a person.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from axiom.extensions.builtins.release.lander.checks import CI_NEVER_RAN, CI_NEVER_RAN_PERSON
from axiom.extensions.builtins.release.lander.config import LanderConfig
from axiom.extensions.builtins.release.lander.model import OpenPull
from axiom.extensions.builtins.release.lander.owner_hold import hold_for
from axiom.infra.state import locked_append_jsonl

ACTION = "ci_start"
RIVET = "rivet"
AUTHOR = "author"
PERSON = "person"


@dataclass(frozen=True)
class Plan:
    """Who acts next on a head CI never ran on, and why."""

    next: str
    why: str

    @property
    def reason(self) -> str:
        """The report reason. Only a pull request nobody else will move needs a person."""
        return CI_NEVER_RAN_PERSON if self.next == PERSON else CI_NEVER_RAN


def ledger_path(state_dir: Path) -> Path:
    return Path(state_dir) / "agents" / "rivet" / "ci-starts.json"


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def starts(state_dir: Path, repo: str, number: int) -> list[dict]:
    """Every start recorded for one pull request, oldest first."""
    rows = _read(ledger_path(state_dir)).get(f"{repo}#{number}")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def spent(state_dir: Path, repo: str, number: int, head_sha: str) -> bool:
    return any(row.get("head_sha") == head_sha for row in starts(state_dir, repo, number))


def record_start(
    state_dir: Path, *, repo: str, number: int, head_sha: str, now: datetime | None = None
) -> None:
    path = ledger_path(state_dir)
    data = _read(path)
    rows = starts(state_dir, repo, number)
    rows.append({"head_sha": head_sha, "at": (now or datetime.now(UTC)).isoformat()})
    data[f"{repo}#{number}"] = rows
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def plan(
    pull: OpenPull,
    config: LanderConfig,
    *,
    state_dir: Path | None,
    repo: str,
    environ: dict[str, str] | None = None,
) -> Plan:
    """Decide the next step for a pull request whose head CI never ran on."""
    env = os.environ if environ is None else environ
    if hold_for(pull, config, repo=repo) is not None:
        # Held for its owner: a close and reopen is a touch, and the hold forbids every one.
        return Plan(AUTHOR, "held for its owner's review; RIVET starts CI once the hold is released")
    if pull.draft:
        return Plan(AUTHOR, "draft: marking it ready starts CI")
    if pull.is_fork:
        return Plan(PERSON, "a fork's pull request is not re-triggered")
    if ACTION not in config.auto_allowed:
        return Plan(PERSON, f"starting CI is not allowed here (raci.auto_allowed lacks {ACTION})")
    if config.ci_start_token_env and not env.get(config.ci_start_token_env):
        return Plan(PERSON, f"no token to start CI with ({config.ci_start_token_env} is empty)")
    if pull.auto_merge:
        return Plan(PERSON, "auto-merge is armed and a close would disarm it")
    if pull.ci_start_spent or (
        state_dir is not None and spent(state_dir, repo, pull.number, pull.head_sha)
    ):
        return Plan(PERSON, "RIVET already started CI once on this head and the checks are still missing")
    if state_dir is not None and len(starts(state_dir, repo, pull.number)) >= config.repair_cap:
        return Plan(PERSON, f"try cap: RIVET started CI {config.repair_cap} times on this pull request")
    return Plan(RIVET, "RIVET starts it once on its next acting sweep")


def sentence(pull: OpenPull, chosen: Plan) -> str:
    """One plain sentence for the report: what is wrong, on which head, and who moves next."""
    names = ", ".join(pull.missing_checks) or "any check"
    tail = f"needs a person: {chosen.why}" if chosen.next == PERSON else chosen.why
    if pull.tests_not_run_cause == "draft_run_only":
        return (
            f"no run started after the pull request was marked ready for {names}; "
            f"head {pull.head_sha[:7]} only has a draft's run; {tail}"
        )
    return f"no workflow run ever started on head {pull.head_sha[:7]} for {names}; {tail}"


def row(pull: OpenPull, chosen: Plan) -> dict:
    """The JSON form of one such pull request."""
    return {
        "number": pull.number,
        "head_sha": pull.head_sha,
        "missing_checks": list(pull.missing_checks),
        "reason": chosen.reason,
        "next": chosen.next,
        "sentence": sentence(pull, chosen),
    }


def raise_to_person(state_dir: Path, repo: str, pull: OpenPull, why: str) -> str:
    """Hand one pull request to a person. The single place this reason is raised.

    Today that is one line in the lander's event log, in the shape the failure
    store already reads, and the reason the report shows. A single "raise an ask"
    call can replace this body without touching its callers.
    """
    locked_append_jsonl(
        Path(state_dir) / "agents" / "rivet" / "lander.jsonl",
        {
            "action": "needs-person",
            "category": CI_NEVER_RAN,
            "repo": repo,
            "pr": pull.number,
            "head_sha": pull.head_sha,
            "missing_checks": list(pull.missing_checks),
            "failure_reason": why,
            "next_route": "human",
        },
    )
    return f"ci never ran: {why}"
