# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Order tests so a likely failure is not queued behind thousands that will pass (#2441).

Failed-first runs what failed in the latest run before anything else. Ranked
order goes further: for independent tests, sorting by *chance of failing divided
by runtime*, descending, minimizes the expected time to the first failure.

The chance of a test failing comes from the recorded history (``order_history``):

- how often it failed when files in the same area changed (hierarchical: the
  area's rate is shrunk toward the test's overall rate, which is shrunk toward
  the suite's, so a test with two runs of evidence does not swing the order);
- recent runs count more (exponential decay in runs);
- a test that failed and passed on the same commit is a flake and counts less;
- a test the history has never seen is new, and new tests fail more often.

Stdlib only. Tests are opaque strings, so another language can use this. The
unit of ordering is the *group*, the part of the id before ``::``, which for
pytest is the module: a module's tests stay together and in their own order, so
module-scoped fixtures are set up once and an order-dependent module keeps the
order it was written for.

Nothing here reads the environment or a file. ``order_plugin`` does.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from axiom.extensions.builtins.release.order_history import (
    RunRecord,
    flaky_ids,
    prefixes_of,
    read_history,
)

MODES = ("off", "failed-first", "ranked")


@dataclass(frozen=True)
class RankConfig:
    #: Runs after which a run's evidence counts half.
    half_life_runs: float = 20.0
    #: Pseudo-runs of prior belief mixed into every estimate.
    prior_runs: float = 2.0
    #: What a flake's chance of failing is multiplied by.
    flake_weight: float = 0.25
    #: Chance of failing for a test the history has never seen.
    unknown_probability: float = 0.02
    #: A test cheaper than this costs this much, so a 0 ms test does not outrank everything.
    duration_floor: float = 0.005
    #: How many recent durations are averaged.
    duration_window: int = 3


def group_of(test_id: str) -> str:
    """The unit that stays together: pytest's module, or whatever precedes ``::``."""
    return test_id.partition("::")[0]


@dataclass
class _Stats:
    weight: float = 0.0
    failures: float = 0.0
    area_weight: float = 0.0
    area_failures: float = 0.0
    last: str = ""
    durations: list[float] | None = None


class Model:
    """What the history says about each test, for one change."""

    def __init__(self, stats: dict[str, _Stats], flakes: set[str], config: RankConfig) -> None:
        self._stats = stats
        self._flakes = flakes
        self._config = config
        weight = sum(s.weight for s in stats.values())
        failures = sum(s.failures for s in stats.values())
        self._prior = max(failures / weight, 1e-5) if weight else config.unknown_probability
        seen = [d for s in stats.values() for d in (s.durations or [])[-1:]]
        self._typical = statistics.median(seen) if seen else config.duration_floor

    def known(self, test_id: str) -> bool:
        return test_id in self._stats

    def last_failed(self, test_id: str) -> bool:
        stats = self._stats.get(test_id)
        return bool(stats and stats.last == "f")

    def probability(self, test_id: str) -> float:
        stats = self._stats.get(test_id)
        if stats is None:
            return self._config.unknown_probability
        m = self._config.prior_runs
        base = (stats.failures + m * self._prior) / (stats.weight + m)
        chance = (stats.area_failures + m * base) / (stats.area_weight + m)
        if test_id in self._flakes:
            chance *= self._config.flake_weight
        return chance

    def seconds(self, test_id: str) -> float:
        stats = self._stats.get(test_id)
        if stats is None or not stats.durations:
            return max(self._typical, self._config.duration_floor)
        window = stats.durations[-self._config.duration_window :]
        return max(sum(window) / len(window), self._config.duration_floor)

    def score(self, test_id: str) -> float:
        return self.probability(test_id) / self.seconds(test_id)


def fit(runs: Sequence[RunRecord], changed_prefixes: Iterable[str], config: RankConfig) -> Model:
    """Learn from ``runs`` (oldest first) for a change touching ``changed_prefixes``."""
    changed = frozenset(changed_prefixes)
    stats: dict[str, _Stats] = defaultdict(_Stats)
    last_index = len(runs) - 1
    for position, record in enumerate(runs):
        weight = 0.5 ** ((last_index - position) / config.half_life_runs)
        in_area = bool(changed & set(record.prefixes))
        for name, outcome, seconds in record.tests:
            entry = stats[name]
            if outcome == "s":
                continue
            failed = outcome == "f"
            entry.weight += weight
            entry.failures += weight if failed else 0.0
            if in_area:
                entry.area_weight += weight
                entry.area_failures += weight if failed else 0.0
            entry.last = outcome
            if entry.durations is None:
                entry.durations = []
            entry.durations.append(seconds)
    return Model(dict(stats), flaky_ids(runs), config)


def _stride(groups: list[str], size: dict[str, int], workers: int, head: int) -> list[str]:
    """Deal ranked groups into ``workers`` bins of equal weight, then join the bins.

    ``head`` tests already sit in front of the list (the last failures), so the
    first bin starts that much full and the bins stay the size xdist will cut.

    xdist's worksteal scheduler gives each worker one contiguous block of the
    collection and runs it front to back. A plain ranked list would put every
    top-ranked group on worker 0. Dealt, each block starts with the next best.
    """
    bins: list[list[str]] = [[] for _ in range(workers)]
    load = [head] + [0] * (workers - 1)
    for name in groups:
        target = min(range(workers), key=lambda b: (load[b], b))
        bins[target].append(name)
        load[target] += size[name]
    return [name for one in bins for name in one]


def order_ids(
    ids: Sequence[str],
    runs: Sequence[RunRecord],
    changed_prefixes: Iterable[str],
    mode: str,
    config: RankConfig | None = None,
    workers: int = 0,
) -> list[str]:
    """``ids`` in the order to run them. Always a permutation of ``ids``."""
    ordered = list(ids)
    if mode == "off" or not runs and mode == "failed-first":
        return ordered
    config = config or RankConfig()
    model = fit(runs, changed_prefixes, config)
    failed = sorted(
        (tid for tid in ordered if model.last_failed(tid)),
        key=lambda tid: (model.seconds(tid), tid),
    )
    failed_set = set(failed)
    rest = [tid for tid in ordered if tid not in failed_set]
    if mode == "failed-first":
        return failed + rest
    if mode != "ranked":
        raise ValueError(f"unknown test order {mode!r}; choose from {MODES}")

    members: dict[str, list[str]] = {}
    for tid in rest:
        members.setdefault(group_of(tid), []).append(tid)

    def group_score(name: str) -> float:
        miss = 1.0
        seconds = 0.0
        for tid in members[name]:
            miss *= 1.0 - model.probability(tid)
            seconds += model.seconds(tid)
        return (1.0 - miss) / seconds

    ranked = sorted(members, key=lambda name: (-group_score(name), name))
    if workers > 1:
        ranked = _stride(
            ranked, {name: len(members[name]) for name in members}, workers, len(failed)
        )
    return failed + [tid for name in ranked for tid in members[name]]


class HistoryOrderer:
    """The ``TestOrderer`` that reads a history file. ``mode`` defaults to ``off``.

    Failed-first and ranked order are opt-in by name: nothing here changes an
    order until a caller asks for one.
    """

    name = "history"

    def __init__(
        self,
        history: Path | str,
        *,
        mode: str = "off",
        config: RankConfig | None = None,
        workers: int = 0,
    ) -> None:
        self.history = Path(history)
        self.mode = mode
        self.config = config
        self.workers = workers

    def order(self, ids: Sequence[str], changed_paths: Sequence[str]) -> list[str]:
        return order_ids(
            ids,
            read_history(self.history),
            prefixes_of(changed_paths),
            self.mode,
            self.config,
            self.workers,
        )
