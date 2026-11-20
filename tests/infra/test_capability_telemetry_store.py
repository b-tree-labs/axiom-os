# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Persisting the capability series is what turns a claim into a number.

Chat, CLI and MCP all publish a content-free projection now, but a bus event
nobody stores measures nothing. This is the store, and it follows the audit
chain's posture deliberately: append-only JSONL under the state dir, so it works
on a laptop with no database. Most installs ARE that laptop, and a measurement
that only works where Postgres does would miss the people we are trying to serve.

Two readers are the point of the exercise:
  * which capabilities have ever been reached for — this feeds the discovery
    block, so usage shrinks it and the recursion closes;
  * how long into a session the first platform call happens, and whether one
    happens at all.
"""

from __future__ import annotations

import json

import pytest

from axiom.infra.capability_telemetry import (
    capability_usage,
    record_capability_event,
    session_discovery_stats,
    telemetry_path,
)


def _ev(tool, *, surface="chat", ok=True, ts=None, session="s1", latency_ms=5):
    return {
        "tool_name": tool, "principal": "@p:local", "surface": surface,
        "ok": ok, "errors_count": 0, "error": "", "latency_ms": latency_ms,
        "args_digest": "d" * 64, "session_id": session, "ts": ts,
    }


def test_an_event_is_appended_and_readable(tmp_path):
    record_capability_event(_ev("data.gold_aggregate"), state_dir=tmp_path)
    lines = telemetry_path(tmp_path).read_text().strip().split("\n")
    assert len(lines) == 1
    assert json.loads(lines[0])["tool_name"] == "data.gold_aggregate"


def test_no_argument_or_result_content_can_be_written(tmp_path):
    """The series inherits the projection's guarantee, and must not become the
    place content leaks back in because a caller passed extra keys."""
    record_capability_event(
        {**_ev("press.draft"), "args": {"token": "SECRET"}, "result": {"v": "SECRET"}},
        state_dir=tmp_path,
    )
    body = telemetry_path(tmp_path).read_text()
    assert "SECRET" not in body
    assert "args" not in json.loads(body.strip())


def test_recording_never_raises_into_the_caller(tmp_path):
    """Same contract as the publisher: telemetry is an observation about a turn,
    never part of it."""
    bad = tmp_path / "not-a-dir"
    bad.write_text("i am a file")
    record_capability_event(_ev("x.y"), state_dir=bad / "nested")  # must not raise


# --- reader one: what feeds the discovery block ------------------------------


def test_usage_reports_every_capability_ever_reached_for(tmp_path):
    for tool in ("data.gold_aggregate", "memory.search", "data.gold_aggregate"):
        record_capability_event(_ev(tool), state_dir=tmp_path)
    assert capability_usage(state_dir=tmp_path) == {"data.gold_aggregate", "memory.search"}


def test_a_failed_invocation_still_counts_as_reached_for(tmp_path):
    """Discovery is about whether someone KNEW to try it. A capability that was
    found and then failed is not a discovery gap — it is a bug."""
    record_capability_event(_ev("press.publish", ok=False), state_dir=tmp_path)
    assert "press.publish" in capability_usage(state_dir=tmp_path)


def test_usage_on_a_fresh_install_is_empty_not_an_error(tmp_path):
    assert capability_usage(state_dir=tmp_path) == set()


# --- reader two: the honest headline metrics ---------------------------------


def test_a_session_with_no_platform_call_is_reported_as_such(tmp_path):
    """This session's own score, and the baseline to beat. It has to be
    reportable rather than inferred from silence."""
    stats = session_discovery_stats(state_dir=tmp_path, sessions=("s-empty",))
    assert stats["sessions"] == 1
    assert stats["sessions_with_a_call"] == 0
    assert stats["first_call_seconds"] == {}


def test_time_to_first_call_is_measured_per_session(tmp_path):
    record_capability_event(_ev("a.b", session="s1", ts=1000.0), state_dir=tmp_path)
    record_capability_event(_ev("c.d", session="s1", ts=1030.0), state_dir=tmp_path)
    stats = session_discovery_stats(state_dir=tmp_path, sessions=("s1",), started_at={"s1": 990.0})
    assert stats["sessions_with_a_call"] == 1
    assert stats["first_call_seconds"]["s1"] == 10.0


def test_negative_control_stats_distinguish_used_from_unused(tmp_path):
    record_capability_event(_ev("a.b", session="s1", ts=5.0), state_dir=tmp_path)
    stats = session_discovery_stats(state_dir=tmp_path, sessions=("s1", "s2"))
    assert stats["sessions"] == 2
    assert stats["sessions_with_a_call"] == 1


# --- the subscriber: published events actually land in the series ------------


def test_subscribing_persists_what_the_surfaces_publish(tmp_path):
    """End to end, because a store nothing writes to is the gap we started with:
    the projection was published and nobody stored it."""
    from axiom.infra.bus.event_bus import EventBus
    from axiom.infra.capability_telemetry import subscribe_capability_telemetry
    from axiom.infra.skill_dispatch import (
        CHAT_SURFACE,
        TELEMETRY_TOPIC,
        publish_capability_telemetry,
    )

    bus = EventBus()
    subscribe_capability_telemetry(bus, state_dir=tmp_path)

    publish_capability_telemetry(
        tool_name="data.gold_aggregate", principal="@p:local", surface=CHAT_SURFACE,
        args={"token": "SECRET"}, result={"ok": True, "errors": []},
        latency_ms=9, eventbus=bus,
    )
    assert TELEMETRY_TOPIC  # the topic is the contract between the two halves
    assert capability_usage(state_dir=tmp_path) == {"data.gold_aggregate"}
    assert "SECRET" not in telemetry_path(tmp_path).read_text()


def test_a_failing_store_does_not_break_the_bus(tmp_path):
    """A subscriber that raises puts the whole payload back on the bus inside
    bus.errors — which is exactly how a content-free design would leak."""
    from axiom.infra.bus.event_bus import EventBus
    from axiom.infra.capability_telemetry import subscribe_capability_telemetry
    from axiom.infra.skill_dispatch import CHAT_SURFACE, publish_capability_telemetry

    blocked = tmp_path / "blocked"
    blocked.write_text("i am a file, not a directory")

    bus = EventBus()
    subscribe_capability_telemetry(bus, state_dir=blocked / "under-a-file")
    publish_capability_telemetry(
        tool_name="x.y", principal="p", surface=CHAT_SURFACE,
        args={}, result={"ok": True}, latency_ms=1, eventbus=bus,
    )  # must not raise


@pytest.fixture(autouse=True)
def _telemetry_on(monkeypatch):
    """The suite runs with the series OFF so it cannot write to the operator's
    real state dir (see the root conftest). This module is the one that tests
    the series, so it turns it back on — against a tmp_path, never the default.
    The two tests that assert the OFF switch set it back themselves, inside the
    test body, which runs after this."""
    monkeypatch.setenv("AXIOM_CAPABILITY_TELEMETRY", "1")


# --- wiring: one call turns the whole loop on ---------------------------------


def test_enabling_is_idempotent(tmp_path):
    """Startup paths get called more than once in practice — a second call must
    not double-record every invocation and silently inflate the measurement."""
    from axiom.infra.bus.event_bus import EventBus
    from axiom.infra.capability_telemetry import enable_capability_telemetry
    from axiom.infra.skill_dispatch import CHAT_SURFACE, publish_capability_telemetry

    bus = EventBus()
    enable_capability_telemetry(state_dir=tmp_path, eventbus=bus)
    enable_capability_telemetry(state_dir=tmp_path, eventbus=bus)
    enable_capability_telemetry(state_dir=tmp_path, eventbus=bus)

    publish_capability_telemetry(
        tool_name="a.b", principal="p", surface=CHAT_SURFACE,
        args={}, result={"ok": True, "errors": []}, latency_ms=1, eventbus=bus,
    )
    lines = [ln for ln in telemetry_path(tmp_path).read_text().splitlines() if ln.strip()]
    assert len(lines) == 1, f"one invocation recorded {len(lines)} times"


def test_the_discovery_block_reads_the_real_install(tmp_path):
    """The bridge: installed capabilities minus what has been used, rendered.
    This is the whole loop in one call, and it is what a startup path invokes."""
    from axiom.infra.capability_telemetry import discovery_block

    class _Spec:
        def __init__(self, name, description, side_effects):
            self.name, self.description, self.side_effects = name, description, side_effects

    class _Registry:
        def __init__(self, specs): self._specs = specs
        def all(self): return list(self._specs)

    registry = _Registry([
        _Spec("data.gold_aggregate", "a total or average over stored data", False),
        _Spec("memory.search", "a decision from an earlier session", False),
    ])

    before = discovery_block(registry, state_dir=tmp_path)
    assert "data.gold_aggregate" in before and "memory.search" in before

    # Two uses: one touch is deliberately not discovery any more, so a stray
    # call cannot hide a capability from everyone forever.
    record_capability_event(_ev("memory.search"), state_dir=tmp_path)
    record_capability_event(_ev("memory.search"), state_dir=tmp_path)
    after = discovery_block(registry, state_dir=tmp_path)
    assert "memory.search" not in after, "a used capability stayed in the block"
    assert "data.gold_aggregate" in after


def test_a_fully_discovered_install_renders_no_block(tmp_path):
    """The end state: nothing left to advertise, so nothing spends context."""
    from axiom.infra.capability_telemetry import discovery_block

    class _Spec:
        def __init__(self, name): self.name, self.description, self.side_effects = name, "x", False
    class _Registry:
        def all(self): return [_Spec("a.b")]

    record_capability_event(_ev("a.b"), state_dir=tmp_path)
    record_capability_event(_ev("a.b"), state_dir=tmp_path)
    assert discovery_block(_Registry(), state_dir=tmp_path) == ""


# --- found by simulating against the real registry, not by unit fixtures -----


def test_a_registry_that_returns_a_mapping_yields_specs_not_names():
    """`SkillRegistry.specs()` returns name -> spec. Listing it yields the NAMES,
    whose .description is empty, so every capability was silently dropped and the
    block rendered empty against a real 57-capability install. Unit fixtures
    returned lists and never caught it."""
    from axiom.infra.capability_telemetry import installed_capabilities

    class _Spec:
        def __init__(self, name): self.name, self.description, self.side_effects = name, "d", False

    class _MappingRegistry:
        def specs(self): return {"a.b": _Spec("a.b"), "c.d": _Spec("c.d")}

    got = installed_capabilities(_MappingRegistry())
    assert [getattr(s, "name", s) for s in got] == ["a.b", "c.d"]
    assert all(hasattr(s, "description") for s in got), "returned names, not specs"


def test_the_publisher_carries_the_session_so_per_session_metrics_work(tmp_path):
    """58 invocations recorded and 0 sessions counted as having used the platform:
    the publisher never carried session_id, so the headline metric could not
    match. The store tests passed because they injected it by hand."""
    from axiom.infra.bus.event_bus import EventBus
    from axiom.infra.capability_telemetry import enable_capability_telemetry
    from axiom.infra.skill_dispatch import CHAT_SURFACE, publish_capability_telemetry

    bus = EventBus()
    enable_capability_telemetry(state_dir=tmp_path, eventbus=bus)
    publish_capability_telemetry(
        tool_name="a.b", principal="p", surface=CHAT_SURFACE, args={},
        result={"ok": True, "errors": []}, latency_ms=2,
        session_id="sess-1", eventbus=bus,
    )
    stats = session_discovery_stats(state_dir=tmp_path, sessions=("sess-1",))
    assert stats["sessions_with_a_call"] == 1


# --- the loop must record from EVERY surface, not just the one it was built on -


def _echo_registry():
    """A real SkillRegistry with one trivial capability."""
    from axiom.infra.skills import SkillRegistry, SkillResult

    registry = SkillRegistry()
    registry.register(
        "probe.echo",
        lambda params, ctx: SkillResult(ok=True, value={"echoed": True}),
        mutating=False,
    )
    return registry


def test_a_cli_invocation_is_recorded(tmp_path):
    """The chokepoint carries CLI, chat and MCP with one capability identity —
    but only chat published on the topic the store subscribes to, so every CLI
    and MCP invocation on every install was measured and then dropped. The
    series looked healthy because the only surface being exercised in tests was
    the one that worked.
    """
    from axiom.infra.bus.event_bus import EventBus
    from axiom.infra.capability_telemetry import enable_capability_telemetry
    from axiom.infra.skill_dispatch import invoke_capability

    bus = EventBus()
    enable_capability_telemetry(state_dir=tmp_path, eventbus=bus)

    result = invoke_capability(
        _echo_registry(), "probe.echo", {}, None, surface="cli", eventbus=bus,
    )
    assert result.ok

    records = [
        __import__("json").loads(ln)
        for ln in telemetry_path(tmp_path).read_text().splitlines()
        if ln.strip()
    ]
    assert [r["tool_name"] for r in records] == ["probe.echo"]
    assert records[0]["surface"] == "cli"


def test_an_mcp_invocation_is_recorded(tmp_path):
    """Same chokepoint, the surface this session's own calls arrive on."""
    from axiom.infra.bus.event_bus import EventBus
    from axiom.infra.capability_telemetry import enable_capability_telemetry
    from axiom.infra.skill_dispatch import invoke_capability

    bus = EventBus()
    enable_capability_telemetry(state_dir=tmp_path, eventbus=bus)
    invoke_capability(
        _echo_registry(), "probe.echo", {}, None, surface="mcp", eventbus=bus,
    )
    assert "probe.echo" in capability_usage(state_dir=tmp_path)


def test_a_cli_invocation_records_no_content(tmp_path):
    """The privacy guarantee has to hold on the path that was not carrying it.
    The gateway payload is wider than the chat publisher's, so this is where a
    leak would enter."""
    from axiom.infra.bus.event_bus import EventBus
    from axiom.infra.capability_telemetry import enable_capability_telemetry
    from axiom.infra.skill_dispatch import invoke_capability

    bus = EventBus()
    enable_capability_telemetry(state_dir=tmp_path, eventbus=bus)
    invoke_capability(
        _echo_registry(), "probe.echo", {"secret_token": "hunter2"}, None,
        surface="cli", eventbus=bus,
    )
    raw = telemetry_path(tmp_path).read_text()
    assert "hunter2" not in raw
    assert "echoed" not in raw


def test_one_invocation_is_recorded_once_not_twice(tmp_path):
    """Both topics now feed the store. A capability that published on both
    would be counted twice, which inflates usage and can silently hide a
    capability from the discovery block after a single real call."""
    from axiom.infra.bus.event_bus import EventBus
    from axiom.infra.capability_telemetry import enable_capability_telemetry
    from axiom.infra.skill_dispatch import invoke_capability

    bus = EventBus()
    enable_capability_telemetry(state_dir=tmp_path, eventbus=bus)
    invoke_capability(
        _echo_registry(), "probe.echo", {}, None, surface="cli", eventbus=bus,
    )
    lines = [ln for ln in telemetry_path(tmp_path).read_text().splitlines() if ln.strip()]
    assert len(lines) == 1, f"one invocation recorded {len(lines)} times"


def _line_count(path):
    return len([ln for ln in path.read_text().splitlines() if ln.strip()])


def test_chat_does_not_double_publish_on_one_bus(tmp_path):
    """The invariant the two-topic store rests on: a surface publishes on
    exactly ONE topic per invocation, per bus.

    Chat holds it by routing its gateway dispatch to its own per-agent bus and
    its projection to the process bus. That is load-bearing, not incidental —
    and it is the kind of thing a later refactor "simplifies" by passing one
    bus everywhere. The negative control below is what makes this a test rather
    than a comment: it shows the failure is real and this shape catches it.
    """
    from axiom.infra.bus.event_bus import EventBus
    from axiom.infra.capability_telemetry import GATEWAY_TOPIC, enable_capability_telemetry
    from axiom.infra.skill_dispatch import CHAT_SURFACE, publish_capability_telemetry

    store_bus, agent_bus = EventBus(), EventBus()
    enable_capability_telemetry(state_dir=tmp_path, eventbus=store_bus)

    # Chat's arrangement: gateway payload to the per-agent bus, projection to
    # the bus the store watches.
    agent_bus.publish(GATEWAY_TOPIC, {"tool_name": "a.b", "principal": "p"},
                      source="tool_gateway")
    publish_capability_telemetry(
        tool_name="a.b", principal="p", surface=CHAT_SURFACE, args={},
        result={"ok": True, "errors": []}, latency_ms=1, eventbus=store_bus,
    )
    assert _line_count(telemetry_path(tmp_path)) == 1


def test_one_bus_for_both_topics_would_double_count(tmp_path):
    """The negative control for the invariant above."""
    from axiom.infra.bus.event_bus import EventBus
    from axiom.infra.capability_telemetry import GATEWAY_TOPIC, enable_capability_telemetry
    from axiom.infra.skill_dispatch import CHAT_SURFACE, publish_capability_telemetry

    bus = EventBus()
    enable_capability_telemetry(state_dir=tmp_path, eventbus=bus)
    bus.publish(GATEWAY_TOPIC, {"tool_name": "a.b", "principal": "p"},
                source="tool_gateway")
    publish_capability_telemetry(
        tool_name="a.b", principal="p", surface=CHAT_SURFACE, args={},
        result={"ok": True, "errors": []}, latency_ms=1, eventbus=bus,
    )
    assert _line_count(telemetry_path(tmp_path)) == 2, (
        "if this ever reads 1, the store grew a dedup and the invariant test "
        "above stopped proving anything"
    )


# --- the loop has to be armed, and has to be refusable ------------------------


def test_telemetry_can_be_switched_off(tmp_path, monkeypatch):
    """This runs on other people's laptops. The series is content-free and never
    leaves the machine, and it is still theirs to decline — a measurement with
    no off switch is one an operator has to uninstall to escape."""
    from axiom.infra.bus.event_bus import EventBus
    from axiom.infra.capability_telemetry import enable_capability_telemetry
    from axiom.infra.skill_dispatch import CHAT_SURFACE, publish_capability_telemetry

    monkeypatch.setenv("AXIOM_CAPABILITY_TELEMETRY", "0")
    bus = EventBus()
    assert enable_capability_telemetry(state_dir=tmp_path, eventbus=bus) is None

    publish_capability_telemetry(
        tool_name="a.b", principal="p", surface=CHAT_SURFACE, args={},
        result={"ok": True, "errors": []}, latency_ms=1, eventbus=bus,
    )
    assert not telemetry_path(tmp_path).exists()


def test_switching_it_off_does_not_break_the_readers(tmp_path, monkeypatch):
    """An install with telemetry off must still answer the discovery question —
    with 'nothing used yet', not an exception."""
    monkeypatch.setenv("AXIOM_CAPABILITY_TELEMETRY", "off")
    assert capability_usage(state_dir=tmp_path) == set()
    assert session_discovery_stats(
        state_dir=tmp_path, sessions=("s1",)
    )["sessions_with_a_call"] == 0


def test_the_cli_arms_the_loop():
    """Nothing called enable_capability_telemetry outside the tests, so the
    store was never subscribed on any real install: every surface published,
    nothing listened, and the series stayed empty while the code that fills it
    was fully tested. A measurement that cannot produce data is worse than none,
    because it reports as built.
    """
    import inspect

    from axiom import axiom_cli

    source = inspect.getsource(axiom_cli.main)
    assert "enable_capability_telemetry" in source, (
        "the CLI entry point does not arm the capability series"
    )


def test_the_mcp_servers_arm_the_loop():
    """The MCP surface is where an assistant's calls arrive, so it is the one
    surface where "did the platform get used this session" is answerable at
    all. An unarmed MCP server publishes into a bus nobody is listening to."""
    import inspect

    from axiom.extensions.builtins.mcp import server as composed
    from axiom.extensions.builtins.memory import mcp_server as memory

    for module in (composed, memory):
        source = inspect.getsource(module.build_server)
        assert "enable_capability_telemetry" in source, (
            f"{module.__name__} does not arm the capability series"
        )


# --- the bench's denominator, taken from what actually happened ---------------


def _seed(tmp_path, latencies, *, surface="cli", tool="a.b"):
    from axiom.infra.capability_telemetry import record_capability_event

    for ms in latencies:
        record_capability_event(
            {"tool_name": tool, "principal": "p", "surface": surface,
             "ok": True, "latency_ms": ms},
            state_dir=tmp_path,
        )


def test_observed_latency_reports_percentiles_and_n(tmp_path):
    """The governance bench states its overhead as a percentage of a *typical*
    100 ms tool call — a number nobody measured. The series holds what calls on
    this install actually cost, which is the denominator the claim wanted."""
    from axiom.infra.capability_telemetry import observed_latency

    _seed(tmp_path, list(range(1, 101)))
    got = observed_latency(state_dir=tmp_path)
    assert got["n"] == 100
    assert got["p50_ms"] == 50
    assert got["p95_ms"] == 95


def test_observed_latency_refuses_to_answer_from_too_few_calls(tmp_path):
    """A p95 over four calls is the fourth-slowest call. Reporting it as a
    percentile would put a shaped number on an unshaped sample, and that number
    would then be quoted."""
    from axiom.infra.capability_telemetry import observed_latency

    _seed(tmp_path, [10, 20, 30, 40])
    got = observed_latency(state_dir=tmp_path, min_samples=30)
    assert got["n"] == 4
    assert got["p50_ms"] is None and got["p95_ms"] is None
    assert "insufficient" in got["note"]


def test_governance_share_uses_the_observed_call_as_the_denominator(tmp_path):
    """Overhead as a fraction of a real call."""
    from axiom.infra.capability_telemetry import governance_share, observed_latency

    _seed(tmp_path, [100] * 50)
    share = governance_share(
        overhead_us=8147.71, observed=observed_latency(state_dir=tmp_path)
    )
    assert round(share["share_of_observed"], 4) == 0.0815
    assert share["observed_p50_ms"] == 100


def test_governance_share_declines_when_the_sample_is_too_small(tmp_path):
    """No observed p50 means no share. Falling back to the assumed 100 ms would
    reproduce the invented denominator while looking like a measurement."""
    from axiom.infra.capability_telemetry import governance_share, observed_latency

    _seed(tmp_path, [100, 100])
    share = governance_share(
        overhead_us=8147.71, observed=observed_latency(state_dir=tmp_path, min_samples=30)
    )
    assert share["share_of_observed"] is None
    assert "insufficient" in share["note"]


def test_the_two_denominators_are_not_interchangeable(tmp_path):
    """The bench divides by BARE work; the observed call already CONTAINS the
    overhead. Quoting one as the other overstates or understates by exactly the
    overhead, and both numbers look like 'percent overhead'."""
    from axiom.infra.capability_telemetry import governance_share, observed_latency

    _seed(tmp_path, [100] * 50)
    share = governance_share(
        overhead_us=8147.71, observed=observed_latency(state_dir=tmp_path)
    )
    assert share["share_of_observed"] < share["share_of_bare_work"]
    assert "denominator" in share["note"]
