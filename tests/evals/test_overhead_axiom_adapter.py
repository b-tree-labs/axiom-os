# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""This platform as one harness among others, with its guarantees declared.

An engine that can measure any framework but not the one that ships it is a
library, not a benchmark. This is the adapter that makes the comparison real —
and the declarations on each variant are the load-bearing part, because timing
these seams against another framework's middleware hook is arithmetic that
produces a number and no meaning.

The adapter uses the ENGINE's shared action body, not the standalone
benchmark's own. That is not a detail: if each harness supplies its own body,
the comparison measures the bodies. It is the reason the adapter constructs its
seams rather than importing the existing script wholesale.
"""

from __future__ import annotations

import pytest

from axiom.evals.adapters import axiom_governance
from axiom.evals.overhead import action_body, compare, measure


@pytest.fixture
def harness(tmp_path):
    return axiom_governance.build(tmp_path)


def test_it_offers_a_bare_baseline(harness):
    """Without it there is nothing to take a delta against, and absolute
    timings across machines are not comparable."""
    assert harness.variant("bare") is not None


def test_every_seam_declares_what_it_buys(harness):
    """A variant with no declared guarantees can be ranked against anything,
    which is how a middleware hook ends up 'beating' a signed receipt."""
    for variant in harness.variants:
        if variant.name == "bare":
            continue
        assert variant.guarantees, f"{variant.name} declares no guarantees"


def test_the_composed_path_claims_a_superset_of_its_parts(harness):
    """`governed_full` is guard + gate + body + receipt. If it claimed fewer
    guarantees than the seams it contains, a reader could conclude the whole
    costs more and buys less than a part."""
    full = set(harness.variant("governed_full").guarantees)
    for part in ("guard", "gate_durable", "receipt"):
        assert set(harness.variant(part).guarantees) <= full, part


def test_it_uses_the_shared_action_body_not_its_own(harness):
    """The property the whole cross-framework comparison rests on."""
    import inspect

    source = inspect.getsource(axiom_governance)
    assert "action_body" in source
    assert "_action_body" not in source, "a private body would break comparability"


def test_the_harness_actually_runs_and_shows_overhead(tmp_path):
    """A small run, for cost. The claim is only that governance costs MORE than
    nothing, which is the one direction that must be true or the seams are not
    running at all."""
    harness = axiom_governance.build(tmp_path)
    result = measure(harness, iterations=25, warmup=3, alloc_iterations=5)
    assert result["deltas_vs_bare"]["guard"]["p50_us"] > 0


def test_it_compares_against_a_harness_with_no_governance(tmp_path):
    """The honest floor, and the shape any competitor comparison takes."""
    from axiom.evals.adapters import null

    ours = measure(axiom_governance.build(tmp_path), iterations=25, warmup=3,
                   alloc_iterations=5)
    theirs = measure(null.build(), iterations=25, warmup=3, alloc_iterations=5)
    report = compare([ours, theirs])
    assert set(report["harnesses"]) == {"axiom", "none"}


def test_unlike_seams_are_not_ranked_against_each_other(tmp_path):
    """The null harness buys nothing; ours buys a policy check and a signed
    receipt. A row that ranked them would say the cheaper one won."""
    from axiom.evals.adapters import null

    ours = measure(axiom_governance.build(tmp_path), iterations=25, warmup=3,
                   alloc_iterations=5)
    theirs = measure(null.build(), iterations=25, warmup=3, alloc_iterations=5)
    row = compare([ours, theirs])["by_variant"].get("governed")
    if row is not None:
        assert row["comparable"] is False


def test_the_shared_body_is_what_bare_measures(harness):
    """`bare` must be the body alone. If it carried any governance, every delta
    would understate the cost by that amount."""
    before = action_body()
    harness.variant("bare").run()
    assert action_body() == before, "the bare variant mutated shared state"
