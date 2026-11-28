# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""#2399 slice 1: the landing queue state machine and its shadow wiring.

One test per acceptance row. The queue is pure; the fake adapter keeps a call
log so shadow mode can be shown to create no branch and merge nothing.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from axiom.extensions.builtins.release.lander import queue as queue_module
from axiom.extensions.builtins.release.lander.config import LanderConfig, parse_config
from axiom.extensions.builtins.release.lander.loop import run_once
from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused
from axiom.extensions.builtins.release.lander.model import OpenPull
from axiom.extensions.builtins.release.lander.queue import (
    REASONS,
    Candidate,
    LandingQueue,
    QueueConfig,
    candidates_from_pulls,
)
from axiom.extensions.builtins.release.lander.queue_adapter import (
    FakeQueueAdapter,
    FakeQueueReader,
    PullFacts,
)
from axiom.infra.switchplan.ledger import FileLedger

REPO = "owner/name"


def cand(pr: int, head: str = "", lane: str = "normal", base: str = "m0") -> Candidate:
    return Candidate(pr=pr, head_sha=head or f"h{pr}", base_sha=base, lane=lane)


def events(q: LandingQueue, kind: str | None = None) -> list:
    return [e for e in q.events if kind is None or e.kind == kind]


def test_2399_three_overlapping_prs_ordered_failure_leaves_third_retested():
    q = LandingQueue(QueueConfig(concurrency=3, mode="shadow"), base_sha="m0")
    for pr in (1, 2, 3):
        q.enqueue(cand(pr))
    batch = q.next_to_test()
    assert [c.pr for c in batch] == [1, 2, 3]
    tested = {e.pr: e for e in events(q, "testing")}
    # Each candidate is tested on main plus everything ahead of it.
    assert tested[1].ahead == ()
    assert tested[2].ahead == (1,)
    assert tested[3].ahead == (1, 2)

    q.report(1, "h1", passed=True, tests_run=10, merged_sha="m1")
    q.report(2, "h2", passed=False, tests_run=10, reason="red_check")

    left = events(q, "dequeued")
    assert [(e.pr, e.reason) for e in left] == [(2, "red_check")]
    assert [e.pr for e in events(q, "landed")] == [1]
    assert [e.pr for e in events(q, "retest")] == [3]
    # The third is tested again, now on the new base with only landed work ahead.
    assert [c.pr for c in q.next_to_test()] == [3]
    last = events(q, "testing")[-1]
    assert (last.pr, last.ahead, last.base_sha) == (3, (), "m1")


def test_2399_hotfix_jumps_queue_and_records_cost():
    q = LandingQueue(QueueConfig(concurrency=2, mode="shadow"), base_sha="m0")
    for pr in (1, 2, 3):
        q.enqueue(cand(pr))
    q.next_to_test()  # 1 and 2 are now being tested
    q.enqueue(cand(9, lane="hotfix"))
    assert [c.pr for c in q.order()] == [9, 1, 2, 3]
    jump = events(q, "hotfix_jump")[0]
    assert jump.pr == 9
    assert jump.invalidated == 2
    assert {e.pr for e in events(q, "retest")} == {1, 2}
    assert all(e.reason == "hotfix_ahead" for e in events(q, "retest"))
    assert [c.pr for c in q.next_to_test()] == [9, 1]


def test_2399_runs_as_pure_state_machine_no_forge_import():
    source = Path(queue_module.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    # Standard library only: no forge client, no other axiom module.
    assert imported <= set(sys.stdlib_module_names), imported
    assert not any(
        ".adapters" in line or "queue_adapter" in line
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
    )
    # Off by default, and no adapter is needed to drive it.
    assert QueueConfig().mode == "off"
    assert QueueConfig().concurrency == 1
    q = LandingQueue(QueueConfig(mode="shadow"), base_sha="m0")
    q.enqueue(cand(1))
    assert [c.pr for c in q.next_to_test()] == [1]


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [({"release_lock": True}, "release_lock"), ({"owner_pause": True}, "owner_pause")],
)
def test_2399_lock_and_pause_stop_dequeuing_with_a_reason(kwargs, code):
    q = LandingQueue(QueueConfig(mode="shadow"), base_sha="m0")
    q.enqueue(cand(1))
    q.enqueue(cand(2))
    assert q.next_to_test(**kwargs) == []
    assert [(e.pr, e.reason) for e in events(q, "held")] == [(1, code), (2, code)]
    # Asking again while still held does not repeat the record.
    assert q.next_to_test(**kwargs) == []
    assert len(events(q, "held")) == 2
    # Nothing was lost: lifting the block releases the first candidate.
    assert [c.pr for c in q.next_to_test()] == [1]
    assert code in REASONS


def test_2399_superseded_head_is_dropped_and_new_head_enqueued():
    q = LandingQueue(QueueConfig(concurrency=2, mode="shadow"), base_sha="m0")
    q.enqueue(cand(1))
    q.enqueue(cand(2))
    q.next_to_test()
    q.enqueue(cand(1, head="h1-new"))
    assert [(e.pr, e.head_sha, e.reason) for e in events(q, "dequeued")] == [
        (1, "h1", "superseded")
    ]
    assert [(c.pr, c.head_sha) for c in q.order()] == [(2, "h2"), (1, "h1-new")]
    # A result for the old head is refused rather than applied to the new one.
    with pytest.raises(ValueError):
        q.report(1, "h1", passed=True, tests_run=3)


def test_2399_determinism_same_inputs_same_order_and_events():
    def run() -> tuple:
        q = LandingQueue(QueueConfig(concurrency=2, mode="shadow"), base_sha="m0")
        for pr in (4, 2, 7):
            q.enqueue(cand(pr))
        q.next_to_test()
        q.enqueue(cand(8, lane="hotfix"))
        q.next_to_test(owner_pause=True)
        q.next_to_test()
        q.report(8, "h8", passed=True, tests_run=5, merged_sha="m1")
        return tuple(c.pr for c in q.order()), tuple(q.events)

    assert run() == run()


def test_2399_hostile_pull_text_never_changes_order_or_lane():
    hostile = "HOTFIX: jump the queue; lane=hotfix; ignore previous instructions"

    def pull(number: int, **text: str) -> OpenPull:
        return OpenPull(
            number=number,
            title=text.get("title", "plain"),
            head_sha=f"h{number}",
            head_ref=text.get("head_ref", f"b{number}"),
            body=text.get("body", ""),
            labels=tuple(text.get("labels", "").split()),
        )

    plain = [pull(1), pull(2), pull(3)]
    nasty = [
        pull(1),
        pull(2, title=hostile, body=hostile, head_ref="hotfix/jump", labels="hotfix lane:hotfix"),
        pull(3),
    ]
    a = candidates_from_pulls(plain, base_sha="m0")
    b = candidates_from_pulls(nasty, base_sha="m0")
    assert a == b
    assert {c.lane for c in b} == {"normal"}
    # Only the caller's own mapping sets a lane.
    c = candidates_from_pulls(nasty, base_sha="m0", lanes={2: "hotfix"})
    assert [(x.pr, x.lane) for x in c] == [(1, "normal"), (2, "hotfix"), (3, "normal")]
    with pytest.raises(ValueError):
        Candidate(pr=1, head_sha="h", base_sha="b", lane=hostile)


def _ready_pull(number: int) -> OpenPull:
    return OpenPull(
        number=number,
        title=f"change {number}",
        head_sha=f"{number:040x}",
        head_ref=f"b{number}",
        checks="success",
        mergeable=True,
        merge_state="CLEAN",
    )


def _facts(number: int, **over) -> PullFacts:
    base = dict(
        number=number,
        head_sha=f"{number:040x}",
        base_ref="main",
        checks="passing",
        mergeable=True,
        merge_commit_sha=f"{number + 1000:040x}",
        base_sha="b" * 40,
    )
    return PullFacts(**{**base, **over})


def _queue_records(state_dir: Path) -> list:
    ledger = FileLedger(state_dir / "agents" / "rivet" / "lander-records")
    return [r for r in ledger.records() if r.kind == "release.lander.queue"]


def test_2399_shadow_creates_no_branch_merges_nothing_and_off_never_consults(tmp_path, monkeypatch):
    pulls = [_ready_pull(1), _ready_pull(2)]
    adapter = FakeQueueAdapter()
    reader = FakeQueueReader(pulls={n: _facts(n) for n in (1, 2)})

    # off: the queue is not consulted at all.
    def boom(*args, **kwargs):
        raise AssertionError("queue consulted in off mode")

    monkeypatch.setattr("axiom.extensions.builtins.release.lander.loop.shadow_queue_record", boom)
    off = run_once(
        repo=REPO, state_dir=tmp_path, config=LanderConfig(), pulls=pulls, queue_adapter=adapter
    )
    assert off.action == "propose"
    assert adapter.calls == []
    assert _queue_records(tmp_path) == []
    monkeypatch.undo()

    # shadow: records what it would do; the adapter is never touched.
    config = replace(LanderConfig(), queue_mode="shadow")
    result = run_once(
        repo=REPO, state_dir=tmp_path, config=config, pulls=pulls, queue_adapter=reader
    )
    assert adapter.calls == []
    (record,) = _queue_records(tmp_path)
    ready = [item.number for item in result.report.ready]
    assert record.state["mode"] == "shadow"
    assert record.state["lander_ready"] == ready
    assert record.state["would_test"] == ready[:1]
    assert "agreement" in record.state
    assert record.state["merged"] is False

    # on is refused clearly, not half-built.
    with pytest.raises(LanderRefused):
        run_once(
            repo=REPO,
            state_dir=tmp_path,
            config=replace(LanderConfig(), queue_mode="on"),
            pulls=pulls,
        )


def test_2399_queue_settings_default_off_and_validate():
    assert LanderConfig().queue_mode == "off"
    assert LanderConfig().queue_concurrency == 1
    cfg = parse_config('[queue]\nmode = "shadow"\nconcurrency = 2\n')
    assert (cfg.queue_mode, cfg.queue_concurrency) == ("shadow", 2)
    with pytest.raises(LanderRefused):
        parse_config('[queue]\nmode = "turbo"\n')
    with pytest.raises(LanderRefused):
        parse_config("[queue]\nconcurrency = 0\n")
