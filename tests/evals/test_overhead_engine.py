# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A governance-overhead number is only interesting next to somebody else's.

The benchmark measured what this platform's gate, receipt and audit layers cost
per action. That is a useful internal number and an unfalsifiable external one:
"8 ms of governance" means nothing until you know what the alternatives charge
for the same guarantees, and the answer might be that we are expensive.

Generalising it is therefore not packaging work. It is what makes the claim
capable of coming out against us.

Two properties carry the whole comparison, and both are easy to lose:

**Every harness must run the IDENTICAL action body.** If each adapter supplies
its own, the comparison measures the bodies. The engine owns it and hands it to
adapters rather than trusting them to agree.

**Seams that are not alike must not be summed.** One framework's "middleware" is
not another's "policy guard plus signed receipt plus audit append". A single
"total overhead" column would quietly compare a subset to a superset, so each
variant declares what guarantee it buys and the report refuses to aggregate
across harnesses that claim different ones.
"""

from __future__ import annotations

import pytest

from axiom.evals.overhead import (
    Harness,
    Variant,
    action_body,
    compare,
    load_adapter,
    measure,
)


def _fast():
    action_body()


def _slow():
    action_body()
    sum(range(4000))


def _harness(name, **variants):
    return Harness(
        name=name,
        variants=[Variant(name=k, run=v, guarantees=("audit",)) for k, v in variants.items()],
    )


def test_it_measures_a_delta_against_the_bare_body():
    result = measure(_harness("demo", bare=_fast, governed=_slow), iterations=60, warmup=5)
    assert result["variants"]["bare"]["p50_us"] > 0
    assert result["deltas_vs_bare"]["governed"]["p50_us"] > 0


def test_an_identical_variant_reports_no_meaningful_overhead():
    """The negative control. A bench that finds overhead between a thing and
    itself is measuring its own noise, and every number after it is that.

    The bound: the p50 delta between a body and itself must stay under the
    body's own p50 (or 1 us, whichever is larger), judged on the median of
    three independent measurements, so one preempted run on a loaded CI worker
    cannot decide it. This once failed at 4.37 us against a 3.87 us bound;
    the cause was the schedule, not the bound: variants were timed in
    consecutive blocks, so drift between blocks landed in the delta. Timing is
    now interleaved (see the schedule test below), which is the actual fix.
    """
    runs = [
        measure(_harness("demo", bare=_fast, same=_fast), iterations=300, warmup=30)
        for _ in range(3)
    ]
    deltas = sorted(abs(r["deltas_vs_bare"]["same"]["p50_us"]) for r in runs)
    bares = sorted(r["variants"]["bare"]["p50_us"] for r in runs)
    delta, bare = deltas[1], bares[1]
    assert delta < max(bare, 1.0), (
        f"median |delta| {delta} us is not noise-sized next to a {bare} us body "
        f"(all three: {deltas})"
    )


def test_variants_are_timed_in_interleaved_rotating_rounds():
    """Why the negative control can hold: every variant is timed in the same
    rounds, so drift in machine speed hits all of them alike rather than
    landing in a delta. Deterministic: it checks the schedule, not a clock."""
    calls: list[str] = []
    variants = {name: (lambda n=name: calls.append(n)) for name in ("bare", "a", "b")}
    measure(_harness("demo", **variants), iterations=3, warmup=0, alloc_iterations=1)
    timed = calls[:9]  # the allocation pass runs after every timed sample
    assert timed == ["bare", "a", "b", "a", "b", "bare", "b", "bare", "a"]


def test_a_harness_without_a_bare_variant_is_refused():
    """Without the shared baseline there is nothing to take a delta against,
    and a harness could otherwise report absolute numbers that look comparable
    and are not."""
    with pytest.raises(ValueError, match="bare"):
        measure(_harness("demo", governed=_slow), iterations=10, warmup=1)


def test_every_harness_shares_one_action_body():
    """If adapters each brought their own, the comparison would measure the
    bodies rather than the governance around them."""
    import inspect

    source = inspect.getsource(action_body)
    assert "def action_body" in source
    first, second = action_body(), action_body()
    assert first == second, "the shared body must be deterministic across calls"


def test_comparison_puts_harnesses_side_by_side():
    a = measure(_harness("alpha", bare=_fast, governed=_slow), iterations=60, warmup=5)
    b = measure(_harness("beta", bare=_fast, governed=_fast), iterations=60, warmup=5)
    report = compare([a, b])
    assert set(report["harnesses"]) == {"alpha", "beta"}
    assert "governed" in report["by_variant"]


def test_it_refuses_to_rank_harnesses_claiming_DIFFERENT_guarantees():
    """The trap this exists to avoid: one framework's middleware hook against
    another's guard + signed receipt + audit append. Ranking those produces a
    number that is arithmetically fine and means nothing."""
    a = measure(
        Harness(name="alpha", variants=[
            Variant(name="bare", run=_fast, guarantees=()),
            Variant(name="governed", run=_slow, guarantees=("audit", "approval", "receipt")),
        ]), iterations=60, warmup=5)
    b = measure(
        Harness(name="beta", variants=[
            Variant(name="bare", run=_fast, guarantees=()),
            Variant(name="governed", run=_fast, guarantees=("audit",)),
        ]), iterations=60, warmup=5)

    report = compare([a, b])
    row = report["by_variant"]["governed"]
    assert row["comparable"] is False
    assert "guarantee" in row["note"].lower()
    assert "ranking" not in report or report.get("ranking") is None


def test_it_DOES_rank_when_the_guarantees_match():
    """The positive control. If it refused every comparison it would be safe
    and useless."""
    a = measure(
        Harness(name="alpha", variants=[
            Variant(name="bare", run=_fast, guarantees=()),
            Variant(name="governed", run=_slow, guarantees=("audit",)),
        ]), iterations=60, warmup=5)
    b = measure(
        Harness(name="beta", variants=[
            Variant(name="bare", run=_fast, guarantees=()),
            Variant(name="governed", run=_fast, guarantees=("audit",)),
        ]), iterations=60, warmup=5)

    row = compare([a, b])["by_variant"]["governed"]
    assert row["comparable"] is True
    assert row["cheapest"] == "beta"


def test_an_adapter_loads_by_dotted_path():
    """Third-party adapters must not require this package to depend on their
    framework. A dotted path keeps a LangGraph adapter out of our imports."""
    build = load_adapter("axiom.evals.adapters.null:build")
    harness = build()
    assert harness.name
    assert any(v.name == "bare" for v in harness.variants)


def test_a_missing_adapter_names_what_it_tried():
    with pytest.raises(ValueError, match="nope.does.not.exist"):
        load_adapter("nope.does.not.exist:build")


def test_the_report_records_what_makes_it_weak():
    """One machine, one process, one run. A benchmark that travels without its
    honesty labels gets quoted as if it were a population estimate."""
    result = measure(_harness("demo", bare=_fast), iterations=30, warmup=5)
    labels = result["methodology"]["honesty_labels"]
    assert any("single machine" in x for x in labels)
    assert result["methodology"]["iterations"] == 30


def test_harnesses_that_share_no_variant_names_are_reported_not_ignored():
    """Found by running the real thing: two harnesses whose variants are named
    differently produce an EMPTY comparison, and the old code returned it
    silently. A comparison that compares nothing and says nothing reads exactly
    like a comparison that found no difference."""
    a = measure(_harness("alpha", bare=_fast, seam_one=_slow), iterations=40, warmup=4)
    b = measure(_harness("beta", bare=_fast, seam_two=_slow), iterations=40, warmup=4)
    report = compare([a, b])
    assert report["by_variant"] == {}
    assert report["comparable_variants"] == []
    assert "no variant" in report["note"].lower()


def test_a_shared_variant_name_clears_the_warning():
    """Negative control: the note must not fire when there IS something to
    compare, or it becomes noise attached to every report."""
    a = measure(_harness("alpha", bare=_fast, governed=_slow), iterations=40, warmup=4)
    b = measure(_harness("beta", bare=_fast, governed=_fast), iterations=40, warmup=4)
    report = compare([a, b])
    assert report["comparable_variants"] == ["governed"]
    assert report["note"] == ""


def test_a_canonical_name_exists_for_the_composed_path():
    """Harnesses can only be compared where their variant names line up, so the
    composed "everything on" path has one agreed name. Without it every adapter
    invents its own and the comparison is empty by construction."""
    from axiom.evals.overhead import COMPOSED_VARIANT

    assert COMPOSED_VARIANT == "governed_full"
