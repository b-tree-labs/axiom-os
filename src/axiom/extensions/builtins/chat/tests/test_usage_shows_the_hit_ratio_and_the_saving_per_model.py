# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/usage`` shows, per model, the cache hit ratio and what caching saved (RS-P11-A4, ADR-272 D6).

A model whose server reported no cache use reads "not reported"; it is never shown as 0%.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.chat.commands import cmd_usage
from axiom.extensions.builtins.chat.usage import TurnUsage, UsageTracker

CLAUDE = "claude-sonnet-4-20250514"


def _tracker(*turns: TurnUsage) -> UsageTracker:
    tracker = UsageTracker()
    for turn in turns:
        tracker.record_turn(turn)
    return tracker


def _row(tracker: UsageTracker, model: str):
    return next(r for r in tracker.cache_by_model() if r.model == model)


def test_a_cached_read_saves_the_difference_from_the_input_price():
    # 1000 prompt tokens, 900 read from the cache at 0.1x: 810 input tokens' worth saved.
    turn = TurnUsage(input_tokens=100, cache_read_tokens=900, cache_creation_tokens=0, model=CLAUDE)
    tracker = _tracker(turn)
    uncached = TurnUsage(input_tokens=1000, model=CLAUDE)
    _tracker(uncached)
    assert turn.cache_saved == pytest.approx(uncached.cost - turn.cost)
    assert turn.cache_saved > 0
    assert _row(tracker, CLAUDE).saved == pytest.approx(turn.cache_saved)


def test_a_write_with_no_read_costs_more_and_says_so():
    turn = TurnUsage(input_tokens=100, cache_read_tokens=0, cache_creation_tokens=900, model=CLAUDE)
    _tracker(turn)
    assert turn.cache_saved < 0


def test_an_unreported_turn_has_no_saving_and_no_ratio():
    tracker = _tracker(TurnUsage(input_tokens=1000, model="gpt-4o"))
    row = _row(tracker, "gpt-4o")
    assert tracker.turns[0].cache_saved is None
    assert (row.hit_ratio, row.saved, row.reported, row.calls) == (None, None, 0, 1)


def test_an_unpriced_model_reports_its_ratio_but_no_saving():
    tracker = _tracker(
        TurnUsage(
            input_tokens=100,
            cache_read_tokens=300,
            cache_creation_tokens=0,
            model="nobody-priced-this",
        )
    )
    row = _row(tracker, "nobody-priced-this")
    assert row.hit_ratio == pytest.approx(0.75)
    assert row.saved is None


def test_the_ratio_of_a_model_is_over_the_calls_that_reported():
    tracker = _tracker(
        TurnUsage(input_tokens=100, cache_read_tokens=900, cache_creation_tokens=0, model=CLAUDE),
        TurnUsage(input_tokens=1000, cache_read_tokens=0, cache_creation_tokens=0, model=CLAUDE),
        TurnUsage(input_tokens=5000, model=CLAUDE),  # said nothing about its cache
    )
    row = _row(tracker, CLAUDE)
    assert (row.calls, row.reported) == (3, 2)
    assert row.hit_ratio == pytest.approx(900 / 2000)


def test_the_saving_survives_a_saved_session():
    tracker = _tracker(
        TurnUsage(input_tokens=100, cache_read_tokens=900, cache_creation_tokens=0, model=CLAUDE)
    )
    back = UsageTracker.from_dict(tracker.to_dict())
    assert back.turns[0].cache_saved == pytest.approx(tracker.turns[0].cache_saved)
    old = TurnUsage.from_dict(
        {"input_tokens": 10, "output_tokens": 5, "model": "gpt-4o", "cost": 0.1, "provider": "p"}
    )
    assert old.cache_saved is None


def _shown(tracker: UsageTracker) -> str:
    return cmd_usage(SimpleNamespace(usage=tracker))


def test_usage_shows_the_ratio_and_the_saving_for_each_model():
    text = _shown(
        _tracker(
            TurnUsage(
                input_tokens=100, cache_read_tokens=900, cache_creation_tokens=0, model=CLAUDE
            ),
            TurnUsage(input_tokens=1000, model="gpt-4o"),
        )
    )
    assert "Cache by model" in text
    claude = next(line for line in text.splitlines() if CLAUDE in line and "%" in line)
    assert "90%" in claude and "$0.0024" in claude
    gpt = next(line for line in text.splitlines() if "gpt-4o" in line and "not reported" in line)
    assert "0%" not in gpt


def test_usage_says_when_caching_cost_more():
    text = _shown(
        _tracker(
            TurnUsage(
                input_tokens=100, cache_read_tokens=0, cache_creation_tokens=900, model=CLAUDE
            )
        )
    )
    assert "cost $" in text and "more" in text
