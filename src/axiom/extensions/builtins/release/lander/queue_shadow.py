# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shadow mode for the landing queue (#2399, slices 1 and 2).

Computes what the queue WOULD do for the pull requests the lander found ready: which
batch it would form, which merged state it would test (a throwaway ref, described and
never made), what it would bisect and remove if that batch came back red, and what the
lander itself did, all in one record next to the lander's own. It reads through a
``QueueReader`` and writes only that record. It creates no branch, merges nothing and
never changes a verdict: the caller treats every failure here as "no shadow record".

Owner pause is the existing kill switch (``config.kill_switch_env``) and the release
lock, not a new setting. An unreadable base or pull request fails closed: no batch.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from axiom.extensions.builtins.release import release_lock
from axiom.extensions.builtins.release.lander.config import LanderConfig
from axiom.extensions.builtins.release.lander.queue import (
    Candidate,
    LandingQueue,
    QueueConfig,
    bisect_plan,
    describe_queue_branch,
    plan_batch,
)
from axiom.extensions.builtins.release.lander.queue_adapter import QueueReader, QueueReadError
from axiom.infra.switchplan.ledger import FileLedger, new_record

KIND = "release.lander.queue"


def lander_actions(report, ready_numbers: list[int]) -> list[dict]:
    """What the existing lander does with each pull request it reported, as data."""
    actions = [{"pr": n, "action": "land", "reason": "ready"} for n in ready_numbers]
    for item in getattr(report, "superseded", []) or []:
        actions.append({"pr": item.number, "action": "skip", "reason": "superseded"})
    for group in ("needs_human", "rivet_handling", "with_author"):
        for item in getattr(report, group, []) or []:
            actions.append({"pr": item.number, "action": "hold", "reason": group})
    return actions


def shadow_queue_record(
    *,
    repo: str,
    state_dir: Path,
    config: LanderConfig,
    ready: list,
    report=None,
    reader: QueueReader | None = None,
    lock_forge: release_lock.LockForge | None = None,
    environ: dict[str, str] | None = None,
    base: str = "main",
) -> dict:
    env = os.environ if environ is None else environ
    lock = release_lock.current(
        state_dir, repo, lock_forge or release_lock.NullLockForge(), now=datetime.now(UTC)
    )
    killed = env.get(config.kill_switch_env, "") == "1"
    paused = killed or config.queue_paused
    lander_ready = [pull.number for pull in ready]

    # -- reads. Anything unreadable is dropped with a reason; nothing is guessed. -----
    base_sha, base_error = "", ""
    excluded: list[dict] = []
    eligible: list[Candidate] = []
    merged_state: dict[int, str] = {}
    if reader is None:
        base_error = "no_reader"
    else:
        try:
            base_sha = reader.base_sha(repo, base)
        except QueueReadError as exc:
            base_error = f"base_unreadable: {exc}"
    if base_sha:
        for pull in ready:
            try:
                facts = reader.pull_facts(repo, pull.number)  # type: ignore[union-attr]
            except QueueReadError:
                excluded.append({"pr": pull.number, "reason": "unreadable"})
                continue
            if facts.head_sha != pull.head_sha:
                excluded.append({"pr": pull.number, "reason": "stale_base"})
            elif facts.mergeable is False:
                excluded.append({"pr": pull.number, "reason": "conflict"})
            elif facts.mergeable is None or not facts.merge_commit_sha:
                excluded.append({"pr": pull.number, "reason": "unreadable"})
            elif facts.checks != "passing":
                excluded.append({"pr": pull.number, "reason": "red_check"})
            else:
                merged_state[pull.number] = facts.merge_commit_sha
                eligible.append(Candidate(pull.number, pull.head_sha, base_sha))

    # -- the queue (slice 1 machine) and the batch plan (slice 2) ----------------------
    queue = LandingQueue(QueueConfig(config.queue_concurrency, "shadow"), base_sha=base_sha)
    for candidate in eligible:
        queue.enqueue(candidate)
    would_test = [
        c.pr for c in queue.next_to_test(release_lock=lock is not None, owner_pause=paused)
    ]
    plan = plan_batch(eligible, base_sha=base_sha, size=config.queue_batch_size)
    blocked = "release_lock" if lock is not None else "owner_pause" if paused else ""
    if base_error:
        plan_reason = base_error
    else:
        plan_reason = plan.reason
    held = [e for e in queue.events if e.kind == "held"]
    actions = (
        lander_actions(report, lander_ready)
        if report is not None
        else [{"pr": n, "action": "land", "reason": "ready"} for n in lander_ready]
    )
    batch = plan.to_dict()
    batch["reason"] = plan_reason or batch["reason"]
    batch["merged_state_shas"] = {str(n): merged_state[n] for n in plan.members}
    batch["would_start"] = bool(plan.members) and not blocked
    batch["blocked_by"] = blocked
    batch["would_push"] = describe_queue_branch(plan, merged_state)
    state = {
        "mode": "shadow",
        "lander_ready": lander_ready,
        "lander_did": actions,
        "would_test": would_test,
        "would_wait": [e.pr for e in held],
        "held_reasons": sorted({e.reason for e in held}),
        "excluded": excluded,
        "batch": batch,
        "if_red": bisect_plan(plan.members),
        "agreement": would_test == lander_ready[: len(would_test)]
        and (bool(would_test) or not lander_ready),
        "events": [
            {"seq": e.seq, "kind": e.kind, "pr": e.pr, "reason": e.reason, "ahead": list(e.ahead)}
            for e in queue.events
        ],
        "merged": False,
        "branches_created": 0,
        "refs_created": 0,
        "writes": 0,
    }
    record = new_record(KIND, repo)
    record.phase = "planned"
    record.state = state
    record.event(
        "shadow",
        f"{len(lander_ready)} ready, batch of {len(plan.members)} on {plan.ref or 'no ref'} "
        f"{'would start' if batch['would_start'] else 'would wait'}",
    )
    FileLedger(Path(state_dir) / "agents" / "rivet" / "lander-records").save(record)
    return state
