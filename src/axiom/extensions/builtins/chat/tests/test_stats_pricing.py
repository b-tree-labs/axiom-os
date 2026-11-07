# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A cost is a price times a count, or it is unknown. Unknown is never $0.00 (ADR-168).

The old table returned 0.0 for any model it did not list, so a newer or self-hosted model summed into
the session total as free and the total silently undercounted. Order of authority: the provider's own
configured price, then "local is free", then the built-in table, else unpriced.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat.usage import (
    TurnUsage,
    UsageTracker,
    compute_cost,
    format_cost,
)
from tests.test_gateway import _make_provider


def test_an_unknown_model_is_unpriced_not_free():
    assert compute_cost("brand-new-model-9", 1000, 500) is None


def test_the_table_still_prices_what_it_lists():
    assert compute_cost("gpt-4o", 1000, 500) == pytest.approx(0.0075)


def test_a_model_name_that_is_only_a_prefix_of_a_listed_one_is_not_priced():
    # The old matcher also tested key.startswith(model), so "gpt" or "o" borrowed a real price.
    assert compute_cost("gpt", 1000, 500) is None
    assert compute_cost("o", 1000, 500) is None


def test_the_longest_listed_name_wins_a_variant():
    # "gpt-4o-mini-2024" starts with gpt-4o, gpt-4o-mini and gpt-4: only the mini price is right.
    assert compute_cost("gpt-4o-mini-2024", 1_000_000, 0) == pytest.approx(0.15)


def test_a_provider_configured_price_beats_the_table():
    p = _make_provider(model="gpt-4o")
    p.cost_per_1k_input = 1.0
    p.cost_per_1k_output = 2.0
    assert compute_cost("gpt-4o", 1000, 1000, provider=p) == pytest.approx(3.0)


def test_a_provider_price_prices_a_model_the_table_does_not_know():
    p = _make_provider(model="house-model")
    p.cost_per_1k_input = 0.5
    p.cost_per_1k_output = 0.5
    assert compute_cost("house-model", 2000, 0, provider=p) == pytest.approx(1.0)


def test_half_a_configured_price_is_not_a_price():
    p = _make_provider(model="house-model")
    p.cost_per_1k_input = 0.5  # no output price
    assert compute_cost("house-model", 1000, 1000, provider=p) is None


def test_a_local_model_is_priced_zero_explicitly():
    p = _make_provider(model="whatever", endpoint="http://localhost:8080/v1", api_key_env="")
    cost = compute_cost("whatever", 5000, 5000, provider=p)
    assert cost == 0.0 and cost is not None


def test_a_cloud_model_with_no_known_price_is_unpriced():
    p = _make_provider(model="whatever", endpoint="https://api.anthropic.com/v1")
    assert compute_cost("whatever", 5000, 5000, provider=p) is None


def test_the_provider_dataclass_carries_the_optional_price_and_window_without_breaking_defaults():
    p = _make_provider()
    assert p.cost_per_1k_input is None
    assert p.cost_per_1k_output is None
    assert p.context_window == 0


def test_the_gateway_loader_reads_price_and_window_from_config(tmp_path, monkeypatch):
    (tmp_path / "llm-providers.toml").write_text(
        '[gateway]\n[[gateway.providers]]\nname = "cfg"\nuid = "uid-cfg"\nendpoint = "https://api.example.com/v1"\nmodel = "m"\n'
        'api_key_env = "FAKE_KEY"\ncost_per_1k_input = 0.25\ncost_per_1k_output = 0.75\n'
        "context_window = 32000\n"
    )
    from unittest.mock import patch

    from axiom.infra.gateway import Gateway

    monkeypatch.setenv("FAKE_KEY", "k")
    with patch("axiom.infra.provider_base.ensure_provider_uids"):  # it rewrites the config file
        gw = Gateway(config_dir=tmp_path)
    prov = next(p for p in gw.providers if p.name == "cfg")
    assert (prov.cost_per_1k_input, prov.cost_per_1k_output, prov.context_window) == (
        0.25,
        0.75,
        32000,
    )


def test_the_tracker_prices_a_turn_with_the_provider_it_was_answered_by():
    local = _make_provider(model="m", endpoint="http://localhost:8080/v1", api_key_env="")
    t = UsageTracker()
    t.record_turn(TurnUsage(input_tokens=10, output_tokens=10, model="m"), provider=local)
    assert t.turns[0].cost == 0.0


def test_an_unpriced_turn_keeps_cost_none_and_is_counted():
    t = UsageTracker()
    t.record_turn(TurnUsage(input_tokens=10, output_tokens=10, model="mystery"))
    t.record_turn(TurnUsage(input_tokens=1000, output_tokens=500, model="gpt-4o"))
    assert t.turns[0].cost is None
    assert t.unpriced_turns == 1
    assert t.total_cost == pytest.approx(0.0075)


def test_a_turn_with_no_model_is_unpriced_too():
    t = UsageTracker()
    t.record_turn(TurnUsage(input_tokens=10, output_tokens=10))
    assert t.turns[0].cost is None and t.unpriced_turns == 1


def test_format_cost_never_prints_a_zero_for_unknown():
    assert format_cost(None) == "unpriced"
    assert format_cost(0.0) == "$0.0000"
    assert format_cost(0.0075) == "$0.0075"


def test_the_total_label_says_how_much_is_missing():
    t = UsageTracker()
    t.record_turn(TurnUsage(input_tokens=10, output_tokens=10, model="mystery"))
    assert t.cost_label() == "unpriced (1 turn unpriced)"
    t.record_turn(TurnUsage(input_tokens=1000, output_tokens=500, model="gpt-4o"))
    assert t.cost_label() == "$0.0075 + 1 turn unpriced"
    t2 = UsageTracker()
    t2.record_turn(TurnUsage(input_tokens=1000, output_tokens=500, model="gpt-4o"))
    assert t2.cost_label() == "$0.0075"


def test_an_old_record_that_says_zero_for_a_model_nobody_priced_loads_as_unpriced():
    old = {"input_tokens": 100, "output_tokens": 50, "model": "mystery", "cost": 0.0}
    assert TurnUsage.from_dict(old).cost is None
    ok = {"input_tokens": 100, "output_tokens": 50, "model": "gpt-4o", "cost": 0.0123}
    assert TurnUsage.from_dict(ok).cost == pytest.approx(0.0123)
