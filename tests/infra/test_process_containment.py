# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Process registry + containment, exercised on REAL throwaway children.

Safety contract for this file:

* Every process signalled here is a child this test spawned, identified by a
  pid we hold and a unique mark scope we set. Nothing is ever matched by name
  or command line, so no developer process can be hit.
* Every child also carries a self-destruct deadline, so even a failing test
  cannot leave a spinner behind.
* The ``spawner`` fixture kills what it spawned at teardown and ASSERTS that
  nothing carrying its mark remains.
* State is a temp dir; the action ledger is the JSONL backend.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from axiom.infra import process_containment as pc
from axiom.infra import process_registry as pr

SLEEP = "import time; time.sleep(60)"
SPIN = "import time\nt=time.time()\nwhile time.time()-t<45: pass"


class Spawner:
    def __init__(self, state: Path):
        self.scope = "t" + uuid.uuid4().hex[:8]
        self.state = state
        self.registry = pr.ProcessRegistry(state, scope=self.scope)
        self.tracked: list[tuple[int, float]] = []  # (pid, start) we own

    def spawn(
        self,
        code: str,
        *,
        new_session: bool = True,
        record_id: str | None = None,
        ready: bool = False,
        mark: bool = True,
    ) -> tuple[subprocess.Popen, str]:
        rid, env = pr.prepare_spawn(scope=self.scope, record_id=record_id)
        if not mark:
            env.pop(pr.MARK_ENV, None)
        p = subprocess.Popen(
            [sys.executable, "-c", code],
            env=env,
            start_new_session=new_session,
            stdout=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
        )
        info = self._wait_probe(p.pid)
        self.tracked.append((p.pid, info.start))
        if ready:
            assert p.stdout.readline().strip() == "ready"
        return p, rid

    @staticmethod
    def _wait_probe(pid: int) -> pr.ProcInfo:
        for _ in range(100):
            info = pr.probe(pid)
            if info:
                return info
            time.sleep(0.05)
        raise AssertionError("child did not start")

    def register(self, p: subprocess.Popen, rid: str, **kw):
        kw.setdefault("kind", "tool")
        kw.setdefault("purpose", "test child")
        return self.registry.register(p.pid, record_id=rid, **kw)

    def track_pid(self, pid: int) -> None:
        self.tracked.append((pid, self._wait_probe(pid).start))

    def cleanup(self) -> None:
        for pid, start in self.tracked:
            info = pr.probe(pid)
            if info and abs(info.start - start) < 2.0:  # still the process we spawned
                for fn in (
                    lambda: os.killpg(info.pgid, signal.SIGKILL) if info.pgid == pid else None,
                    lambda: os.kill(pid, signal.SIGKILL),
                ):
                    try:
                        fn()
                    except (ProcessLookupError, PermissionError):
                        pass
        time.sleep(0.1)


@pytest.fixture
def sp(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.delenv("AXIOM_DB_URL", raising=False)
    s = Spawner(tmp_path / "state")
    yield s
    s.cleanup()
    # The property this fixture exists for: nothing we started is still alive.
    leftovers = [
        pid for pid, start in s.tracked if (i := pr.probe(pid)) and abs(i.start - start) < 2.0
    ]
    assert leftovers == [], f"stray processes survived the test: {leftovers}"
    assert pr.scan_marks(scope=s.scope) == {}, "stray marked processes remain"


def _gone(pid: int, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pr.probe(pid) is None:
            return True
        time.sleep(0.05)
    return False


def _alive(pid: int) -> bool:
    return pr.probe(pid) is not None


def _reap(p: subprocess.Popen) -> None:
    try:
        p.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


def _ledger(sp: Spawner) -> list[dict]:
    from axiom.policy.action_ledger import ActionLedger

    return ActionLedger(state_dir=sp.state).query(limit=500)


# ---------------------------------------------------------------- registry


def test_record_is_atomic_json_with_identity_fields(sp):
    p, rid = sp.spawn(SLEEP)
    rec = sp.register(
        p,
        rid,
        principal="@ben:local",
        agent="alpha",
        extension="x",
        purpose="p",
        max_runtime_s=5,
        cpu_budget_s=2,
        rss_budget_mb=100,
    )
    data = json.loads((sp.registry.dir / f"{rid}.json").read_text())
    for key in (
        "pid",
        "start_time",
        "parent_pid",
        "max_runtime_s",
        "cpu_budget_s",
        "rss_budget_mb",
        "principal",
        "agent",
        "extension",
        "purpose",
    ):
        assert key in data
    assert data["pid"] == p.pid and data["start_time"] == rec.start_time
    assert not list(sp.registry.dir.glob(".*.tmp")), "no temp files left behind"


def test_clean_exit_deregisters_and_running_process_keeps_record(sp):
    p, rid = sp.spawn("import time; time.sleep(1.5)")
    with pr.registered_process(
        p, registry=sp.registry, kind="tool", purpose="short", record_id=rid
    ):
        assert sp.registry.get(rid) is not None
        p.wait(timeout=5)
    assert sp.registry.get(rid) is None, "exited process must deregister"

    q, rid2 = sp.spawn(SLEEP)
    with pr.registered_process(
        q, registry=sp.registry, kind="task", purpose="long", record_id=rid2
    ):
        pass
    assert sp.registry.get(rid2) is not None, "still-running detached process keeps its record"


def test_damaged_record_does_not_hide_the_others(sp):
    p, rid = sp.spawn(SLEEP)
    sp.register(p, rid)
    (sp.registry.dir / "garbage.json").write_text("{not json")
    recs, damaged = sp.registry.load()
    assert [r.id for r in recs] == [rid] and damaged == ["garbage.json"]


# ---------------------------------------------------------------- detection


def test_a_exceeding_max_runtime_is_found_then_stopped(sp):
    p, rid = sp.spawn(SLEEP)
    rec = sp.register(p, rid, max_runtime_s=0.3)
    time.sleep(1.5)
    rep = pc.detect_rogues(sp.registry, scan_escaped=False)
    assert [f.kind for f in rep.findings] == ["overrun"]
    # report mode signals nothing
    pc.sweep(sp.registry, mode="report")
    assert _alive(p.pid)
    # dry-run signals nothing
    res = pc.terminate(rec, state_dir=sp.state, why="t", execute=False)
    assert res["action"] == "dry-run" and _alive(p.pid)
    res = pc.terminate(rec, state_dir=sp.state, why="t", execute=True, grace_s=3)
    assert res["action"] == "terminated"
    _reap(p)
    assert _gone(p.pid)


def test_b_cpu_spinner_over_budget_is_found_and_stopped(sp):
    p, rid = sp.spawn(SPIN)
    rec = sp.register(p, rid, cpu_budget_s=0.4)
    end = time.time() + 15
    while time.time() < end and (pr.probe(p.pid).cpu_s <= 0.4):
        time.sleep(0.2)
    rep = pc.detect_rogues(sp.registry, scan_escaped=False)
    kinds = {f.kind for f in rep.findings}
    assert "cpu_over_budget" in kinds
    pc.terminate(rec, state_dir=sp.state, why="t", execute=True, grace_s=3)
    _reap(p)
    assert _gone(p.pid)


def test_under_budget_and_in_time_process_is_not_flagged(sp):
    p, rid = sp.spawn(SLEEP)
    sp.register(p, rid, max_runtime_s=300, cpu_budget_s=300, rss_budget_mb=4000)
    assert pc.detect_rogues(sp.registry, scan_escaped=False).findings == []


PARENT = textwrap.dedent("""
    import subprocess, sys, time
    c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                         start_new_session=True)
    print(c.pid, flush=True)
    time.sleep(60)
""")


def test_c_orphan_after_registered_parent_dies_and_auto_class(sp):
    parent, prid = sp.spawn(PARENT)
    child_pid = int(parent.stdout.readline())
    sp.track_pid(child_pid)
    crid = uuid.uuid4().hex[:12]
    rec = sp.registry.register(
        child_pid,
        kind="tool",
        purpose="orphan-to-be",
        record_id=crid,
        parent_pid=parent.pid,
        max_runtime_s=0.3,
    )
    svc_rid = uuid.uuid4().hex[:12]
    svc = sp.registry.register(
        child_pid,
        kind="service",
        purpose="same pid, service kind",
        record_id=svc_rid,
        parent_pid=parent.pid,
        max_runtime_s=0.3,
    )
    kinds = [f.kind for f in pc.detect_rogues(sp.registry, scan_escaped=False).findings]
    assert "orphan" not in kinds, "a live parent means no orphan"
    parent.kill()
    _reap(parent)
    time.sleep(1.0)  # past 2x the 0.3s max runtime
    rep = pc.detect_rogues(sp.registry, scan_escaped=False)
    by_id = {f.record_id: f for f in rep.findings if f.kind == "orphan"}
    assert by_id[crid].auto is True, "orphaned tool past 2x max runtime is auto-class"
    assert by_id[svc_rid].auto is False, "a service is never auto-class"
    sp.registry.deregister(svc_rid)
    del svc
    # auto mode contains it; nothing else is touched
    out = pc.sweep(sp.registry, mode="auto")
    assert [a["action"] for a in out["actions"]] in (["terminated"], ["killed"])
    assert _gone(child_pid)
    assert rec.id not in {r.id for r in sp.registry.records()}, "record reaped"


def test_orphan_is_not_auto_when_policy_is_off(sp):
    parent, _ = sp.spawn(PARENT)
    child_pid = int(parent.stdout.readline())
    sp.track_pid(child_pid)
    sp.registry.register(
        child_pid, kind="tool", purpose="x", parent_pid=parent.pid, max_runtime_s=0.1
    )
    parent.kill()
    _reap(parent)
    time.sleep(0.6)
    pol = pc.ContainmentPolicy(auto=frozenset())
    out = pc.sweep(sp.registry, mode="auto", policy=pol)
    assert out["actions"] == [] and _alive(child_pid)
    assert any(f["kind"] == "orphan" for f in out["findings"])


def test_d_pid_reuse_is_never_signalled(sp):
    # The reused-pid process is a stranger: it carries no harness mark.
    bystander, rid = sp.spawn(SLEEP, mark=False)
    live = pr.probe(bystander.pid)
    # A record that names this pid but a different start time: what a reused pid looks like.
    fake = pr.ProcessRecord(
        id=rid,
        pid=bystander.pid,
        start_time=live.start - 3600,
        pgid=live.pgid,
        own_group=True,
        uid=live.uid,
        kind="tool",
        purpose="stale",
        scope=sp.scope,
        max_runtime_s=0.01,
        parent_pid=1,
    )
    sp.registry._write(fake)
    time.sleep(0.2)
    rep = pc.detect_rogues(sp.registry, scan_escaped=False)
    assert [f.kind for f in rep.findings] == ["pid_reused"]
    assert not rep.findings[0].killable
    res = pc.terminate(fake, state_dir=sp.state, why="t", execute=True, grace_s=0.2)
    assert res["action"] == "refused" and res["signalled"] is False
    pc.sweep(sp.registry, mode="kill")
    assert _alive(bystander.pid), "a process whose start time disagrees must survive everything"
    refused = [r for r in _ledger(sp) if r["outcome"] == "refused"]
    assert refused and refused[0]["refusing_rule"] == "verify_ours", "refusals are audited"


def test_d_mutation_guard_is_what_protects_the_bystander(sp, monkeypatch):
    """Control: with the start-time guard neutered, the same fake record WOULD
    verify. Proves test_d can fail, i.e. that the guard is the thing under test."""
    b, rid = sp.spawn(SLEEP)
    live = pr.probe(b.pid)
    fake = pr.ProcessRecord(
        id=rid,
        pid=b.pid,
        start_time=live.start - 3600,
        pgid=live.pgid,
        own_group=True,
        uid=live.uid,
        kind="tool",
        purpose="s",
        scope=sp.scope,
    )
    assert pr.verify_ours(fake)[0] is False
    monkeypatch.setattr(pr, "START_TOLERANCE_S", 1e9)
    assert pr.verify_ours(fake)[0] is True, "mutant accepts a reused pid; the guard is load-bearing"


def test_e_sigterm_ignorer_needs_sigkill(sp):
    code = (
        "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\ntime.sleep(60)"
    )
    p, rid = sp.spawn(code, ready=True)
    rec = sp.register(p, rid)
    res = pc.terminate(rec, state_dir=sp.state, why="t", execute=True, grace_s=0.5)
    assert res["action"] == "killed", "SIGTERM alone must not have been enough"
    _reap(p)
    assert _gone(p.pid)
    stop = [r for r in _ledger(sp) if r["op_class"] == "process.contain.stop"]
    assert stop and stop[0]["metadata"]["evidence"]["result"] == "killed"


def test_cooperative_child_is_terminated_without_sigkill(sp):
    p, rid = sp.spawn(SLEEP)
    rec = sp.register(p, rid)
    res = pc.terminate(rec, state_dir=sp.state, why="t", execute=True, grace_s=5)
    assert res["action"] == "terminated"
    _reap(p)


FORKER = textwrap.dedent("""
    import subprocess, sys, time
    kids = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            for _ in range(2)]
    print(" ".join(str(k.pid) for k in kids), flush=True)
    time.sleep(60)
""")


def test_f_group_kill_takes_the_grandchildren(sp):
    p, rid = sp.spawn(FORKER)
    kids = [int(x) for x in p.stdout.readline().split()]
    for k in kids:
        sp.track_pid(k)
    rec = sp.register(p, rid)
    assert rec.own_group
    res = pc.terminate(rec, state_dir=sp.state, why="t", execute=True, grace_s=5)
    assert res["group"] is True
    _reap(p)
    assert _gone(p.pid) and all(_gone(k) for k in kids), "grandchildren must die with the group"


def test_f_control_without_group_only_the_leader_dies(sp):
    p, rid = sp.spawn(FORKER)
    kids = [int(x) for x in p.stdout.readline().split()]
    for k in kids:
        sp.track_pid(k)
    rec = sp.register(p, rid)
    rec.own_group = False  # a record that did not lead its own group
    res = pc.terminate(rec, state_dir=sp.state, why="t", execute=True, grace_s=5)
    assert res["group"] is False
    _reap(p)
    assert _gone(p.pid)
    assert all(_alive(k) for k in kids), "no group signal means grandchildren are untouched"


def test_never_signals_own_group_or_ancestors(sp):
    me = pr.probe(os.getpid())
    rec = pr.ProcessRecord(
        id="self",
        pid=os.getpid(),
        start_time=me.start,
        pgid=me.pgid,
        own_group=True,
        uid=me.uid,
        kind="tool",
        purpose="x",
        scope=sp.scope,
    )
    res = pc.terminate(rec, state_dir=sp.state, why="t", execute=True, grace_s=0.1)
    assert res["action"] == "refused"


# ---------------------------------------------------------------- marks


def test_unregistered_marked_child_found_but_bystander_and_descendants_are_not(sp):
    escaped, _ = sp.spawn(SLEEP)  # marked, no record
    bystander, _ = sp.spawn(SLEEP, mark=False)  # same shape, no mark: must be invisible
    registered, rid = sp.spawn(FORKER)
    kids = [int(x) for x in registered.stdout.readline().split()]
    for k in kids:
        sp.track_pid(k)
    sp.register(registered, rid)
    rep = pc.detect_rogues(sp.registry)
    flagged = {f.pid for f in rep.findings if f.kind == "unregistered"}
    assert flagged == {escaped.pid}
    assert bystander.pid not in flagged and not (set(kids) & flagged)
    # report leaves everything alone; kill takes only the escaped one
    pc.sweep(sp.registry, mode="report")
    assert _alive(escaped.pid)
    pc.sweep(sp.registry, mode="kill", policy=pc.ContainmentPolicy(grace_s=3))
    _reap(escaped)
    assert _gone(escaped.pid)
    assert _alive(bystander.pid) and _alive(registered.pid) and all(_alive(k) for k in kids)


def test_duplicate_singleton_flags_the_newer_instance(sp):
    a, ra = sp.spawn(SLEEP)
    sp.register(a, ra, singleton_key="svc:x", kind="service")
    time.sleep(1.2)
    b, rb = sp.spawn(SLEEP)
    sp.register(b, rb, singleton_key="svc:x", kind="service")
    dups = [
        f
        for f in pc.detect_rogues(sp.registry, scan_escaped=False).findings
        if f.kind == "duplicate_singleton"
    ]
    assert [f.record_id for f in dups] == [rb]
    assert dups[0].auto is False


# ---------------------------------------------------------------- agents


def _seed(sp: Spawner, agent: str, n: int, outcome: str = "proceeded") -> None:
    from axiom.policy.action_ledger import ActionLedger

    led = ActionLedger(state_dir=sp.state)
    for i in range(n):
        led.record_action(
            agent=agent, op_class="x.y", name="y", candidate=str(i), guards=[], outcome=outcome
        )


def _enable(*names: str) -> None:
    from axiom.extensions.builtins.agents.consent import record_decision

    record_decision(enabled=list(names))


def test_g_action_rate_runaway_turns_consent_off_only_for_that_agent(sp):
    _enable("alpha", "beta")
    _seed(sp, "alpha", 30)
    _seed(sp, "beta", 3)
    from axiom.policy.action_ledger import ActionLedger

    rows = ActionLedger(state_dir=sp.state).query(limit=500)
    pol = pc.ContainmentPolicy(runaway_max_actions=20)
    out = pc.sweep(
        sp.registry, mode="kill", policy=pol, ledger_rows=rows, enabled_agents={"alpha", "beta"}
    )
    assert [f["kind"] for f in out["findings"]] == ["runaway_agent_rate"]
    from axiom.extensions.builtins.agents.consent import load_consent

    assert load_consent().enabled == ["beta"]
    audit = [r for r in _ledger(sp) if r["op_class"] == "process.contain.agent_pause"]
    assert audit and audit[0]["candidate"] == "alpha" and audit[0]["outcome"] == "proceeded"
    assert "runaway_agent_rate" in audit[0]["metadata"]["why"]
    # report mode never changes consent
    _enable("alpha", "beta")
    pc.sweep(
        sp.registry, mode="report", policy=pol, ledger_rows=rows, enabled_agents={"alpha", "beta"}
    )
    assert load_consent().enabled == ["alpha", "beta"]
    # resume restores exactly the paused agent
    _enable("beta")
    pc._write_paused(sp.state, {"alpha": {"why": "t"}})
    pc.resume_agent("alpha", state_dir=sp.state, execute=True)
    assert sorted(load_consent().enabled) == ["alpha", "beta"]


def test_g_failure_streak_and_threshold_and_already_off(sp):
    from axiom.policy.action_ledger import ActionLedger

    _seed(sp, "alpha", 6, outcome="failed")
    _seed(sp, "gamma", 6, outcome="refused")  # guards doing their job: not a failure
    rows = ActionLedger(state_dir=sp.state).query(limit=500)
    pol = pc.ContainmentPolicy(runaway_max_failures=5)
    kinds = {(f.agent, f.kind) for f in pc.detect_runaway_agents(rows, {"alpha", "gamma"}, pol)}
    assert kinds == {("alpha", "runaway_agent_failures")}
    assert pc.detect_runaway_agents(rows, set(), pol) == [], "an agent already off is no finding"
    below = pc.ContainmentPolicy(runaway_max_failures=50)
    assert pc.detect_runaway_agents(rows, {"alpha"}, below) == []


# ---------------------------------------------------------------- panic


def test_panic_dry_run_changes_nothing_then_execute_stops_agent_work(sp):
    _enable("alpha", "beta")
    agent_p, ra = sp.spawn(SLEEP)
    sp.register(agent_p, ra, agent="alpha", kind="agent")
    tool_p, rt = sp.spawn(SLEEP)
    sp.register(tool_p, rt, kind="tool")
    from axiom.extensions.builtins.agents.consent import load_consent

    out = pc.panic(sp.registry)
    assert out["mode"] == "dry-run" and _alive(agent_p.pid)
    assert sorted(load_consent().enabled) == ["alpha", "beta"]
    out = pc.panic(sp.registry, execute=True, grace_s=3)
    _reap(agent_p)
    assert load_consent().enabled == []
    assert _gone(agent_p.pid), "agent-owned process stopped"
    assert _alive(tool_p.pid), "panic stops agent-owned work, not every registered tool"
    assert {a["agent"] for a in out["agents"]} == {"alpha", "beta"}


def test_panic_with_damaged_registry_falls_back_to_marks_report_only_by_default(sp):
    marked, _ = sp.spawn(SLEEP)
    bystander, _ = sp.spawn(SLEEP, mark=False)
    sp.registry.dir.mkdir(parents=True, exist_ok=True)
    (sp.registry.dir / "broken.json").write_text("\x00\x01 not json")
    out = pc.panic(sp.registry, enabled_agents=[])
    assert out["fallback"] is True and out["damaged"] == ["broken.json"]
    assert [p["pid"] for p in out["processes"]] == [marked.pid]
    assert out["processes"][0]["action"] == "dry-run" and _alive(marked.pid)
    pc.panic(sp.registry, execute=True, grace_s=3, enabled_agents=[])
    _reap(marked)
    assert _gone(marked.pid)
    assert _alive(bystander.pid), "an unmarked process is never touched"


def test_panic_survives_an_unreadable_registry_directory(sp, monkeypatch):
    marked, _ = sp.spawn(SLEEP)
    monkeypatch.setattr(sp.registry, "load", lambda: (_ for _ in ()).throw(OSError("boom")))
    out = pc.panic(sp.registry, enabled_agents=[])
    assert out["fallback"] and marked.pid in [p["pid"] for p in out["processes"]]


def test_policy_defaults_are_conservative():
    pol = pc.ContainmentPolicy()
    assert pol.auto == frozenset({"orphans"})
    assert pol.orphan_factor == 2.0


def test_timestamps_in_rows_without_tz_are_handled():
    rows = [
        {
            "agent": "a",
            "ts": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "outcome": "proceeded",
        }
        for _ in range(5)
    ]
    pol = pc.ContainmentPolicy(runaway_max_actions=2)
    assert pc.detect_runaway_agents(rows, {"a"}, pol)[0].kind == "runaway_agent_rate"
