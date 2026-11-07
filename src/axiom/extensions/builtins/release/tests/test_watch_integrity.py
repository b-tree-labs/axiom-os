# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Green requires having looked.

The three fixtures are the shape the real failure had, with the names made
generic: one repository that EXISTS and is the wrong one (a public mirror,
pushed daily, so it looks alive), one that 404s, and the private one where
the work actually happens. Naming the real repositories put a consumer into
a platform test, which the mirror guard caught — and it was the only thing
wrong in the tree.

Written against a real failure: RIVET reported all_green on 17,513 of
17,545 heartbeats while the repositories it named were a public mirror and
a 404. Every test here is a sentence that was false on 2026-09-29.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.release.watch_integrity import (
    BLIND,
    GREEN,
    LIVE,
    MISSING,
    MOVED,
    RED,
    UNPARSEABLE,
    UNREACHABLE,
    WatchTarget,
    drift,
    observe,
    render,
    resolve_targets,
)

REAL = "acme/product-private"
MIRROR = "acme/product"
GONE = "acme/product-legacy"


def target(ident=REAL, **over):
    return WatchTarget(identifier=ident, source="watched-repos.toml", **over)


def resolver(table):
    def _resolve(t: WatchTarget):
        return table.get(t.identifier)
    return _resolve


# --- resolution never drops a target ---------------------------------------


def test_a_live_repo_resolves_to_itself():
    [r] = resolve_targets([target()], resolver({REAL: REAL}))
    assert r.state == LIVE and r.observing


def test_a_missing_repo_is_a_finding_not_a_skip():
    """The loader's comment said bad URLs are 'silently skipped'. That is
    the entire defect: skipping turned a config mistake into a green light."""
    [r] = resolve_targets([target(GONE)], resolver({}))
    assert r.state == MISSING
    assert not r.observing
    assert GONE in r.describe()


def test_a_moved_repo_names_both_names():
    """The commonest cause is a rename, and a host redirects rather than
    404ing — so the old name looks fine until you read what came back."""
    [r] = resolve_targets([target(MIRROR)], resolver({MIRROR: REAL}))
    assert r.state == MOVED
    assert r.resolved_to == REAL
    assert "MOVED" in r.describe() and MIRROR in r.describe() and REAL in r.describe()


def test_the_config_that_named_it_is_in_the_message():
    """A finding without an address is a finding nobody can act on."""
    [r] = resolve_targets([target(GONE)], resolver({}))
    assert "watched-repos.toml" in r.describe()


@pytest.mark.parametrize("bad", ["", "axiom-os", "not a repo"])
def test_something_that_is_not_a_repo_reference_says_so(bad):
    [r] = resolve_targets([target(bad)], resolver({}))
    assert r.state == UNPARSEABLE


def test_a_host_that_is_down_is_told_apart_from_a_repo_that_is_gone():
    """One is the network, the other the configuration. Collapsing them
    sends somebody to fix the wrong thing."""
    def boom(_t):
        raise TimeoutError("api.github.com timed out")
    [r] = resolve_targets([target()], boom)
    assert r.state == UNREACHABLE
    assert "timed out" in r.why


def test_every_target_comes_back_even_when_most_are_broken():
    targets = [target(REAL), target(GONE), target("junk")]
    assert len(resolve_targets(targets, resolver({REAL: REAL}))) == 3


# --- green requires having looked ------------------------------------------


def test_the_exact_heartbeat_that_was_wrong_is_now_blind():
    """Watch list: one mirror, one 404. Pipelines seen: zero. RIVET said
    all_green. It must say blind, and say why."""
    resolutions = resolve_targets(
        [target(MIRROR), target(GONE)], resolver({MIRROR: REAL}),
    )
    obs = observe(resolutions, pipelines_seen=0, failures_seen=0)

    assert obs.state == BLIND
    assert obs.all_green is False
    assert any("MOVED" in r for r in obs.reasons)
    assert any(GONE in r for r in obs.reasons)


def test_zero_pipelines_is_blind_even_when_every_repo_resolves():
    """Watching the right repo on a ref where nothing runs looks identical
    to peace, and is not."""
    obs = observe(resolve_targets([target()], resolver({REAL: REAL})),
                  pipelines_seen=0, failures_seen=0)
    assert obs.state == BLIND
    assert "no pipelines were observed" in obs.reasons[0]


def test_an_empty_watch_list_is_blind_not_green():
    obs = observe([], pipelines_seen=0, failures_seen=0)
    assert obs.state == BLIND
    assert "no repositories are configured" in obs.reasons[0]


def test_one_broken_target_is_enough_to_withhold_green():
    """A partial green reads as a whole one."""
    resolutions = resolve_targets([target(REAL), target(GONE)], resolver({REAL: REAL}))
    obs = observe(resolutions, pipelines_seen=40, failures_seen=0)
    assert obs.state == BLIND
    assert obs.targets_live == 1 and obs.targets_total == 2


def test_green_is_only_green_with_targets_and_pipelines_and_no_failures():
    obs = observe(resolve_targets([target()], resolver({REAL: REAL})),
                  pipelines_seen=12, failures_seen=0)
    assert obs.state == GREEN and obs.all_green is True


def test_failures_read_as_red_not_blind():
    obs = observe(resolve_targets([target()], resolver({REAL: REAL})),
                  pipelines_seen=12, failures_seen=3)
    assert obs.state == RED and obs.all_green is False


def test_all_green_is_computed_so_nothing_can_assert_it():
    obs = observe([], pipelines_seen=0, failures_seen=0)
    with pytest.raises(AttributeError):
        obs.all_green = True  # type: ignore[misc]


# --- the other direction: work that moved away -----------------------------


def test_a_repo_being_pushed_to_but_not_watched_is_named():
    """Checking the entries you have cannot reveal the one you never added,
    which is what a migration looks like from here."""
    resolutions = resolve_targets([target(MIRROR)], resolver({MIRROR: MIRROR}))
    assert drift(resolutions, [REAL, MIRROR]) == [REAL]


def test_drift_follows_a_rename_rather_than_reporting_it_twice():
    resolutions = resolve_targets([target(MIRROR)], resolver({MIRROR: REAL}))
    assert drift(resolutions, [REAL]) == []


def test_nothing_drifts_when_the_list_matches_reality():
    resolutions = resolve_targets([target()], resolver({REAL: REAL}))
    assert drift(resolutions, [REAL]) == []


# --- how it reads -----------------------------------------------------------


def test_blind_says_it_is_not_a_clean_run():
    out = render(observe([], pipelines_seen=0, failures_seen=0))
    assert "BLIND" in out
    assert "not a clean run; it is no run" in out


def test_green_says_how_much_it_looked_at():
    out = render(observe(resolve_targets([target()], resolver({REAL: REAL})),
                         pipelines_seen=12, failures_seen=0))
    assert "12 pipeline(s)" in out and "1 repo(s)" in out


def test_drift_appears_in_the_report():
    resolutions = resolve_targets([target(MIRROR)], resolver({MIRROR: MIRROR}))
    out = render(observe(resolutions, pipelines_seen=1, failures_seen=0), drift(resolutions, [REAL]))
    assert "pushed to but not watched" in out and REAL in out
