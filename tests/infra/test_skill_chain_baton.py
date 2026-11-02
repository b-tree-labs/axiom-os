"""A chain of hand-offs may loop and may run long; it always ends, for a reason it can name.

There is no hop limit: a long job with many legitimate hand-offs must not be
cut off by a count. Each of the deterministic exits is shown here, plus the
two things a person needs: seeing the chains in flight, and stopping one.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from axiom.extensions.builtins.agents.skills import bind as bind_agents
from axiom.infra import chain_ledger, skill_chain
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec


def _ctx(reg: SkillRegistry, tmp_path: Path) -> SkillContext:
    return SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("test"))


def _spec(name, fn, risk="read"):
    return SkillSpec(
        name=name,
        fn=fn,
        description=name,
        surfaces=("cli",),
        side_effects=risk != "read",
        risk=risk,
        requires=(),
    )


def _chained(reg, tmp_path, name, params=None, chain_id="c1", **chain):
    return invoke_capability(
        reg,
        name,
        params or {},
        _ctx(reg, tmp_path),
        surface=CLI_SURFACE,
        chain={"chain_id": chain_id, **chain},
    )


def test_a_long_chain_of_distinct_calls_is_never_cut_off_by_a_count(tmp_path):
    reg = SkillRegistry()
    reg.register_skill(
        _spec("work.step", lambda p, c: SkillResult(ok=True, value={"item": p["item"]})),
        mutating=False,
    )
    for i in range(250):
        assert _chained(reg, tmp_path, "work.step", {"item": i}).ok, i
    assert chain_ledger.load(tmp_path, "c1").hops == 250


def test_recursion_that_never_returns_stops_at_the_identical_call_backstop(tmp_path):
    calls: list[str] = []
    reg = SkillRegistry()

    def make(name, other):
        def run(params, ctx):
            calls.append(name)
            baton = skill_chain.baton_for(ctx)
            why = baton.refusal(ctx, other, {}, writes=False)
            if why:
                return SkillResult(ok=True, value={"stopped_at": name, "why": why})
            inner = invoke_capability(
                ctx.registry, other, {}, ctx, surface=CLI_SURFACE, chain=baton.passing_to(other)
            )
            return SkillResult(ok=True, value={"inner": inner.value})

        return run

    reg.register_skill(_spec("ping.a", make("ping.a", "ping.b")), mutating=False)
    reg.register_skill(_spec("ping.b", make("ping.b", "ping.a")), mutating=False)
    r = _chained(reg, tmp_path, "ping.a", chain_id="loop")
    assert r.ok
    assert calls == ["ping.a", "ping.b"] * chain_ledger.MAX_IDENTICAL
    inner = r.value
    while "inner" in inner:
        inner = inner["inner"]
    assert "same arguments" in inner["why"] and "without a result to compare" in inner["why"]


def test_the_same_call_with_the_same_result_twice_is_no_progress(tmp_path):
    reg = SkillRegistry()
    reg.register_skill(
        _spec("poll.status", lambda p, c: SkillResult(ok=True, value={"state": "pending"})),
        mutating=False,
    )
    assert _chained(reg, tmp_path, "poll.status").ok
    assert _chained(reg, tmp_path, "poll.status").ok
    third = _chained(reg, tmp_path, "poll.status")
    assert (
        not third.ok
        and third.unmet[0]["id"] == "chain-ended"
        and "no progress" in third.unmet[0]["why"]
    )


def test_changing_results_still_meet_the_identical_call_backstop(tmp_path):
    reg = SkillRegistry()
    counter = iter(range(100))
    reg.register_skill(
        _spec("poll.count", lambda p, c: SkillResult(ok=True, value={"n": next(counter)})),
        mutating=False,
    )
    for _ in range(chain_ledger.MAX_IDENTICAL):
        assert _chained(reg, tmp_path, "poll.count").ok
    assert (
        f"started {chain_ledger.MAX_IDENTICAL} times"
        in _chained(reg, tmp_path, "poll.count").unmet[0]["why"]
    )


def test_a_deadline_ends_the_chain_and_a_new_chain_gets_the_default(tmp_path):
    reg = SkillRegistry()
    reg.register_skill(
        _spec("work.step", lambda p, c: SkillResult(ok=True, value=p)), mutating=False
    )
    past = str(int(time.time()) - 5)
    r = _chained(reg, tmp_path, "work.step", {"i": 1}, chain_id="late", deadline=past)
    assert not r.ok and "passed its deadline" in r.unmet[0]["why"]
    _chained(reg, tmp_path, "work.step", {"i": 2}, chain_id="fresh")
    rec = chain_ledger.load(tmp_path, "fresh")
    assert abs((rec.deadline - rec.started) - chain_ledger.default_deadline_hours() * 3600) < 5


def test_critical_resources_refuse_writes_but_not_reads(tmp_path):
    reg = SkillRegistry()
    reg.register_skill(
        _spec("data.write", lambda p, c: SkillResult(ok=True, value=p), risk="reversible-write"),
        mutating=True,
    )
    reg.register_skill(
        _spec("data.read", lambda p, c: SkillResult(ok=True, value=p)), mutating=False
    )
    vit = tmp_path / chain_ledger.VITALS_FILE
    vit.parent.mkdir(parents=True)
    vit.write_text(
        json.dumps({"verdict": "critical", "at": time.time(), "critical_mounts": ["/home"]})
    )
    w = _chained(reg, tmp_path, "data.write", {"x": 1})
    assert (
        not w.ok
        and w.unmet[0]["id"] == "chain-deferred"
        and "KEEL's vitals are critical (/home)" in w.unmet[0]["why"]
    )
    assert _chained(reg, tmp_path, "data.read", {"x": 1}).ok
    vit.write_text(
        json.dumps({"verdict": "critical", "at": time.time() - 7200, "critical_mounts": ["/home"]})
    )
    assert _chained(reg, tmp_path, "data.write", {"x": 2}).ok  # a stale verdict is not evidence


def test_agents_can_list_chains_and_stop_one_between_steps(tmp_path):
    reg = SkillRegistry()
    bind_agents(reg)
    reg.register_skill(
        _spec("work.step", lambda p, c: SkillResult(ok=True, value=p)), mutating=False
    )
    assert _chained(reg, tmp_path, "work.step", {"i": 1}, chain_id="job7").ok
    ctx = _ctx(reg, tmp_path)
    listed = reg.invoke("agents.chains", {}, ctx)
    row = next(r for r in listed.value["chains"] if r["chain_id"] == "job7")
    assert row["status"] == "running" and row["hops"] == 1 and row["last_capability"] == "work.step"
    stopped = reg.invoke(
        "agents.chain_stop", {"id": "job7", "by": "@ben:local", "reason": "wrong target"}, ctx
    )
    assert stopped.ok and stopped.value["chain"]["status"] == "stopping"
    nxt = _chained(reg, tmp_path, "work.step", {"i": 2}, chain_id="job7")
    assert not nxt.ok and "stopped by @ben:local: wrong target" in nxt.unmet[0]["why"]
    assert not reg.invoke("agents.chain_stop", {"id": "nope"}, ctx).ok


def test_the_chain_normaliser_keeps_the_deadline_and_drops_junk():
    assert skill_chain.normalise_chain(
        {"chain_id": "c1", "deadline": "1767225600", "hops": "9"}
    ) == {
        "chain_id": "c1",
        "deadline": "1767225600",
    }
    assert skill_chain.normalise_chain({"chain_id": "c1", "deadline": "soon"}) == {"chain_id": "c1"}


# -- priority, strategies, no starvation, no deadlock --------------------------------


def _vitals(tmp_path, verdict, mounts=("/home",)):
    vit = tmp_path / chain_ledger.VITALS_FILE
    vit.parent.mkdir(parents=True, exist_ok=True)
    key = "critical_mounts" if verdict == "critical" else "pressure_mounts"
    vit.write_text(json.dumps({"verdict": verdict, "at": time.time(), key: list(mounts)}))


def _writer_registry():
    reg = SkillRegistry()
    reg.register_skill(
        _spec("data.write", lambda p, c: SkillResult(ok=True, value=p), risk="reversible-write"),
        mutating=True,
    )
    return reg


def test_under_pressure_background_waits_and_the_rest_share_by_priority(tmp_path):
    reg = _writer_registry()
    for cid, prio in (("bg", "background"), ("n1", "normal"), ("h1", "high"), ("n2", "normal")):
        assert _chained(
            reg, tmp_path, "data.write", {"c": cid, "i": 0}, chain_id=cid, priority=prio
        ).ok
    _vitals(tmp_path, "pressure")
    assert (
        "background chains wait"
        in _chained(reg, tmp_path, "data.write", {"c": "bg", "i": 1}, chain_id="bg").unmet[0]["why"]
    )
    # two writers under pressure (the default): high first, then the oldest normal
    assert _chained(reg, tmp_path, "data.write", {"c": "h1", "i": 1}, chain_id="h1").ok
    assert _chained(reg, tmp_path, "data.write", {"c": "n1", "i": 1}, chain_id="n1").ok
    waited = _chained(reg, tmp_path, "data.write", {"c": "n2", "i": 1}, chain_id="n2")
    assert (
        not waited.ok
        and waited.unmet[0]["id"] == "chain-deferred"
        and "h1 (high)" in waited.unmet[0]["why"]
    )
    assert chain_ledger.load(tmp_path, "n2").status == "running"  # deferred, not ended


def test_strategies_order_competing_chains_differently():
    now = 10_000.0
    old_normal = chain_ledger.ChainRecord(
        chain_id="old", started=now - 900, deadline=now + 9e4, priority="normal", last_write=now
    )
    new_high = chain_ledger.ChainRecord(
        chain_id="new", started=now - 60, deadline=now + 9e4, priority="high", last_write=now
    )

    def first(strategy):
        return sorted([old_normal, new_high], key=lambda r: chain_ledger._order(strategy, r, now))[
            0
        ].chain_id

    assert first("priority") == "new"
    assert first("finish-first") == "old"
    assert first("interactive-first") == "new"


def test_a_deferred_chain_ages_so_it_cannot_starve(tmp_path):
    now = time.time()
    rec = chain_ledger.ChainRecord(
        chain_id="bg",
        started=now - 7200,
        deadline=now + 9e4,
        priority="background",
        deferred_since=now - chain_ledger.AGE_STEP_S - 1,
    )
    assert chain_ledger.effective_priority(rec, now) == "normal"
    rec.deferred_since = now - 5 * chain_ledger.AGE_STEP_S
    assert chain_ledger.effective_priority(rec, now) == "high"  # never ages into critical
    assert (
        chain_ledger.admit(rec, [], "pressure", "/home", strategy="priority", writers=1, now=now)
        == ""
    )


def test_the_way_out_of_pressure_is_never_deferred(tmp_path):
    reg = SkillRegistry()
    reg.register_skill(
        _spec("node.clean", lambda p, c: SkillResult(ok=True, value=p), risk="reversible-write"),
        mutating=True,
    )
    reg.register_skill(
        _spec(
            "notifications.alert",
            lambda p, c: SkillResult(ok=True, value=p),
            risk="reversible-write",
        ),
        mutating=True,
    )
    _vitals(tmp_path, "critical")
    assert _chained(
        reg, tmp_path, "node.clean", {"plan": 1}, chain_id="fix", priority="background"
    ).ok
    assert _chained(
        reg, tmp_path, "notifications.alert", {"s": 1}, chain_id="tell", priority="background"
    ).ok


def test_some_writer_always_progresses_under_pressure(tmp_path):
    reg = _writer_registry()
    ids = [f"c{i}" for i in range(10)]
    for cid in ids:
        _chained(reg, tmp_path, "data.write", {"c": cid, "i": 0}, chain_id=cid, priority="normal")
    _vitals(tmp_path, "pressure")
    admitted = [
        cid
        for cid in ids
        if _chained(reg, tmp_path, "data.write", {"c": cid, "i": 1}, chain_id=cid).ok
    ]
    assert 1 <= len(admitted) <= chain_ledger.DEFAULT_PRESSURE_WRITERS


def test_priority_can_be_raised_while_a_chain_waits(tmp_path):
    reg = _writer_registry()
    bind_agents(reg)
    for cid, prio in (("a", "high"), ("b", "high"), ("c", "normal")):
        _chained(reg, tmp_path, "data.write", {"c": cid, "i": 0}, chain_id=cid, priority=prio)
    _vitals(tmp_path, "pressure")
    assert not _chained(reg, tmp_path, "data.write", {"c": "c", "i": 1}, chain_id="c").ok
    r = reg.invoke(
        "agents.chain_priority",
        {"id": "c", "priority": "critical", "by": "@ben:local"},
        _ctx(reg, tmp_path),
    )
    assert r.ok and r.value["chain"]["priority"] == "critical"
    assert _chained(reg, tmp_path, "data.write", {"c": "c", "i": 2}, chain_id="c").ok


# -- orphans and the baton's lease ---------------------------------------------------


def test_a_lapsed_lease_orphans_the_chain_and_its_children(tmp_path):
    reg = SkillRegistry()
    bind_agents(reg)
    reg.register_skill(
        _spec("work.step", lambda p, c: SkillResult(ok=True, value=p)), mutating=False
    )
    assert _chained(reg, tmp_path, "work.step", {"i": 1}, chain_id="parent").ok
    assert _chained(reg, tmp_path, "work.step", {"i": 1}, chain_id="kid", parent_chain="parent").ok
    rec = chain_ledger.load(tmp_path, "parent")
    rec.lease_until = time.time() - chain_ledger.LEASE_GRACE_S - 10  # its holder died mid-step
    chain_ledger.save(tmp_path, rec)
    listed = reg.invoke("agents.chains", {}, _ctx(reg, tmp_path))
    assert set(listed.value["orphaned_now"]) == {"parent", "kid"}
    status = {r["chain_id"]: r["status"] for r in listed.value["chains"]}
    assert status == {"parent": "orphaned", "kid": "orphaned"}
    nxt = _chained(reg, tmp_path, "work.step", {"i": 2}, chain_id="kid")
    assert not nxt.ok and nxt.unmet[0]["id"] == "chain-ended" and "orphaned" in nxt.unmet[0]["why"]


def test_stopping_a_parent_ends_its_children_at_their_next_hop(tmp_path):
    reg = SkillRegistry()
    bind_agents(reg)
    reg.register_skill(
        _spec("work.step", lambda p, c: SkillResult(ok=True, value=p)), mutating=False
    )
    _chained(reg, tmp_path, "work.step", {"i": 1}, chain_id="job")
    child = skill_chain.Baton(chain_id="job").child(priority="background")
    assert child["parent_chain"] == "job" and child["priority"] == "background"
    assert _chained(reg, tmp_path, "work.step", {"i": 1}, **child).ok
    reg.invoke("agents.chain_stop", {"id": "job", "by": "@ben:local"}, _ctx(reg, tmp_path))
    r = _chained(reg, tmp_path, "work.step", {"i": 2}, **child)
    assert not r.ok and "parent job is stopping" in r.unmet[0]["why"]


def test_a_long_step_renews_its_baton(tmp_path):
    reg = SkillRegistry()
    reg.register_skill(
        _spec("work.step", lambda p, c: SkillResult(ok=True, value=p)), mutating=False
    )
    _chained(reg, tmp_path, "work.step", {"i": 1}, chain_id="long")
    rec = chain_ledger.load(tmp_path, "long")
    rec.lease_until = time.time() - 1
    chain_ledger.save(tmp_path, rec)
    assert chain_ledger.renew(tmp_path, "long")
    assert chain_ledger.sweep(tmp_path) == []
    assert chain_ledger.load(tmp_path, "long").lease_until > time.time()
