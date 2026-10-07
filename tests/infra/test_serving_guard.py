# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The serving guard bounds what any one call can cost (ADR-157).

Every test here is a clause of the PRD's principles: no call is unbounded, a
limit never discards the part the caller most likely wants, the response says
what was done first, the caller cannot widen their own limits, and refusal is
cheap. The negative controls prove the guard can fail — a battery that cannot
fail is decoration.
"""

from __future__ import annotations

from axiom.infra import serving_guard as sg


def _policy(**over):
    p = sg.ServingPolicy.shipped_defaults()
    return p.replace(**over) if over else p


# ---------------------------------------------------------------- caller class


def test_surfaces_map_to_caller_classes():
    assert sg.caller_class(surface="mcp", assured=False) == "agent"
    assert sg.caller_class(surface="agent_tool", assured=False) == "agent"
    assert sg.caller_class(surface="cli", assured=False) == "interactive"
    assert sg.caller_class(surface="web", assured=True) == "interactive"
    assert sg.caller_class(surface="chat", assured=False) == "interactive"


def test_an_unknown_surface_is_anonymous():
    # Principle 7: anonymous gets the smallest budget. An unmapped transport
    # must never inherit a larger one by accident.
    assert sg.caller_class(surface=None, assured=False) == "anonymous"
    assert sg.caller_class(surface="something-new", assured=False) == "anonymous"


# ---------------------------------------------------------------- policy table


def test_shipped_defaults_cover_every_class_pair():
    p = sg.ServingPolicy.shipped_defaults()
    for caller in ("agent", "interactive", "service", "anonymous"):
        for cost in ("lookup", "aggregate", "series"):
            b = p.budget(cost_class=cost, caller_class=caller)
            assert b.default_points > 0
            assert b.max_points >= b.default_points
            assert b.max_bytes > 0


def test_anonymous_budgets_are_the_smallest():
    p = sg.ServingPolicy.shipped_defaults()
    for cost in ("lookup", "aggregate", "series"):
        anon = p.budget(cost_class=cost, caller_class="anonymous")
        for caller in ("agent", "interactive", "service"):
            other = p.budget(cost_class=cost, caller_class=caller)
            assert anon.max_points <= other.max_points
            assert anon.max_bytes <= other.max_bytes


def test_a_policy_file_overrides_a_number_but_not_the_shape(tmp_path):
    f = tmp_path / "serving_policy.toml"
    f.write_text(
        '[budgets.agent.series]\ndefault_points = 100\nmax_points = 500\n',
        encoding="utf-8",
    )
    p = sg.load_policy(f)
    assert p.budget(cost_class="series", caller_class="agent").max_points == 500
    # untouched entries keep shipped defaults
    assert (
        p.budget(cost_class="series", caller_class="service").max_points
        == sg.ServingPolicy.shipped_defaults()
        .budget(cost_class="series", caller_class="service")
        .max_points
    )


def test_a_request_cannot_carry_its_own_limits():
    # Principle 6. The admit path takes the policy and the context; nothing it
    # reads comes from request params. This is structural: admit() has no
    # params argument at all.
    import inspect

    sig = inspect.signature(sg.admit)
    assert "params" not in sig.parameters


# ---------------------------------------------------------------------- admit


def _admit(verb="data.series", cost="series", caller="agent", principal="@p",
           policy=None, guard=None):
    g = guard or sg.ServingGuard(policy or _policy())
    return g, g.admit(
        verb=verb, cost_class=cost, caller_class=caller, principal=principal
    )


def test_a_declared_verb_within_budget_is_admitted():
    _, outcome = _admit()
    assert outcome.allowed
    assert outcome.budget.max_points > 0


def test_a_verb_without_a_cost_class_is_refused():
    # Principle 5: a verb with no declaration is refused — before the database.
    _, outcome = _admit(cost=None)
    assert not outcome.allowed
    assert "cost class" in outcome.reason


def test_a_suspended_principal_is_refused_at_once():
    p = _policy(suspended_principals=frozenset({"@bad"}))
    _, outcome = _admit(principal="@bad", policy=p)
    assert not outcome.allowed
    assert "suspended" in outcome.reason


def test_the_rate_limit_refuses_the_flood_not_the_first_call():
    g = sg.ServingGuard(_policy())
    outcomes = []
    for _ in range(50):  # sequential calls: each returns before the next
        o = g.admit(verb="data.series", cost_class="series",
                    caller_class="anonymous", principal="@flood")
        o.release()
        outcomes.append(o)
    assert outcomes[0].allowed
    refused = [o for o in outcomes if not o.allowed]
    assert refused, "50 instant calls from one anonymous principal must trip it"
    assert "rate" in refused[0].reason


def test_rate_buckets_are_per_principal():
    g = sg.ServingGuard(_policy())
    for _ in range(50):
        g.admit(verb="data.series", cost_class="series",
                caller_class="anonymous", principal="@flood")
    fresh = g.admit(verb="data.series", cost_class="series",
                    caller_class="anonymous", principal="@quiet")
    assert fresh.allowed, "one principal's flood must not starve another"


def test_concurrency_is_capped_and_released():
    g = sg.ServingGuard(_policy())
    cap = _policy().limits("anonymous").max_concurrent
    held = []
    for _ in range(cap):
        o = g.admit(verb="data.series", cost_class="series",
                    caller_class="anonymous", principal="@par")
        assert o.allowed
        held.append(o)
    over = g.admit(verb="data.series", cost_class="series",
                   caller_class="anonymous", principal="@par")
    assert not over.allowed and "concurrent" in over.reason
    held[0].release()
    again = g.admit(verb="data.series", cost_class="series",
                    caller_class="anonymous", principal="@par")
    assert again.allowed


# ------------------------------------------------------------------ preflight


def test_a_series_with_no_window_is_refused_and_names_the_cheaper_call():
    # Principle 9: refusal is cheap and precise. Full history at full
    # resolution is never a valid question to the serving tier.
    plan = sg.preflight_series(
        window_seconds=None, bucket_seconds=60.0,
        budget=_policy().budget(cost_class="series", caller_class="agent"),
    )
    assert plan.refused
    assert "window" in plan.reason
    assert "aggregate" in plan.cheaper_call


def test_a_fitting_request_passes_through_unchanged():
    plan = sg.preflight_series(
        window_seconds=3600.0, bucket_seconds=60.0,
        budget=_policy().budget(cost_class="series", caller_class="agent"),
    )  # 60 points
    assert not plan.refused and not plan.reshaped
    assert plan.bucket_seconds == 60.0


def test_an_oversized_request_is_reshaped_across_the_whole_window():
    # Principle 2: a 24h ask at 10 Hz comes back as 24h of coarser buckets,
    # never as the oldest 8.3 minutes.
    budget = _policy().budget(cost_class="series", caller_class="agent")
    plan = sg.preflight_series(
        window_seconds=86400.0, bucket_seconds=0.1, budget=budget
    )
    assert plan.reshaped and not plan.refused
    assert plan.bucket_seconds >= 86400.0 / budget.max_points
    assert 86400.0 / plan.bucket_seconds <= budget.max_points


def test_a_microsecond_bucket_is_hostile_and_still_bounded():
    budget = _policy().budget(cost_class="series", caller_class="agent")
    plan = sg.preflight_series(
        window_seconds=86400.0, bucket_seconds=1e-6, budget=budget
    )
    assert plan.reshaped
    assert 86400.0 / plan.bucket_seconds <= budget.max_points


# ------------------------------------------------------------------ bounding


def test_even_sampling_keeps_ends_and_shape():
    rows = list(range(1000))
    out = sg.sample_evenly(rows, 100)
    assert len(out) == 100
    assert out[0] == 0 and out[-1] == 999
    assert out == sorted(out)


def test_sampling_under_budget_is_identity():
    rows = list(range(10))
    assert sg.sample_evenly(rows, 100) == rows


def test_byte_bounding_is_honest():
    big = {"data": {"series": [{"t": i, "value": i} for i in range(10000)]}}
    bounded, note = sg.bound_bytes(big, max_bytes=4096)
    import json

    assert len(json.dumps(bounded)) <= 4096
    assert note is not None and "reduced" in note
    small = {"data": {"value": 1}}
    same, no_note = sg.bound_bytes(small, max_bytes=4096)
    assert same == small and no_note is None


# ---------------------------------------------------------- the served block


def test_the_served_block_states_what_was_done():
    served = sg.served_block(
        requested_window_seconds=86400.0,
        requested_bucket_seconds=0.1,
        effective_bucket_seconds=43.2,
        returned_points=2000,
        covered=("2024-04-01T00:00:00", "2024-04-02T00:00:00"),
        reduced=True,
    )
    assert served["reduced"] is True
    assert served["returned_points"] == 2000
    assert served["resolution_seconds"] == 43.2
    assert served["requested"]["bucket_seconds"] == 0.1
    assert served["covered"]["start"] == "2024-04-01T00:00:00"


# ------------------------------------------------------------------ metering


def test_every_admit_is_metered_per_principal():
    g = sg.ServingGuard(_policy())
    g.admit(verb="data.series", cost_class="series",
            caller_class="agent", principal="@a")
    g.admit(verb="data.series", cost_class="series",
            caller_class="agent", principal="@a")
    refused = None
    for _ in range(200):
        o = g.admit(verb="data.series", cost_class="series",
                    caller_class="anonymous", principal="@b")
        if not o.allowed:
            refused = o
            break
    m = g.metering()
    assert m["@a"]["admitted"] == 2
    assert refused is not None and m["@b"]["refused"] >= 1


# ------------------------------------------------------- negative control


def test_negative_control_the_guard_can_fail():
    # A policy stripped of its anonymous rate limit admits the flood — proving
    # the refusals above come from the policy, not from an accident.
    p = _policy()
    unlimited = p.replace(
        limits_table={**p.limits_table,
                      "anonymous": p.limits("anonymous").replace(
                          rate_per_second=1e9, burst=1e9)},
    )
    g = sg.ServingGuard(unlimited)
    outcomes = []
    for _ in range(50):
        o = g.admit(verb="data.series", cost_class="series",
                    caller_class="anonymous", principal="@flood")
        o.release()
        outcomes.append(o)
    assert all(o.allowed for o in outcomes)


def test_refusal_does_not_touch_a_database():
    # Principle 9 structurally: admit and preflight import nothing that can
    # open a connection.
    import axiom.infra.serving_guard as mod

    src = open(mod.__file__, encoding="utf-8").read()
    for needle in ("psycopg2", "session_for", "create_engine"):
        assert needle not in src
