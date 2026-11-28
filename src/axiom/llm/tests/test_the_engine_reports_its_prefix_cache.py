# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A self-hosted engine's prefix cache is read from its own metrics (#2212, RS-H11).

vLLM does not reliably fill ``cached_tokens`` per response, so a model reusing its prefix
looks uncached to the gateway. The engine's Prometheus metrics have the truth.
"""

from __future__ import annotations

import pytest

from axiom.llm.engine_cache import EngineCache, parse_metrics

VLLM_V1 = """\
# HELP vllm:prefix_cache_queries_total Prefix cache queries, in terms of number of queried tokens.
# TYPE vllm:prefix_cache_queries_total counter
vllm:prefix_cache_queries_total{engine="0",model_name="qwen"} 2.33081761e+08
vllm:prefix_cache_queries_created{engine="0",model_name="qwen"} 1.7948058421449747e+09
vllm:prefix_cache_hits_total{engine="0",model_name="qwen"} 2.113584e+08
vllm:kv_cache_usage_perc{engine="0",model_name="qwen"} 0.25
"""

VLLM_V0 = """\
vllm:gpu_prefix_cache_hit_rate{model_name="qwen"} 0.42
vllm:gpu_cache_usage_perc{model_name="qwen"} 0.1
"""

SGLANG = """\
sglang:cache_hit_rate{model_name="m"} 0.66
sglang:token_usage{model_name="m"} 0.5
"""


def test_vllm_hits_over_queries_is_the_hit_rate():
    cache = parse_metrics(VLLM_V1)
    assert cache == EngineCache(
        engine="vllm",
        hit_rate=pytest.approx(211358400 / 233081761),
        kv_cache_used=0.25,
        queried_tokens=233081761,
    )


def test_an_older_vllm_reports_its_rate_directly():
    cache = parse_metrics(VLLM_V0)
    assert (cache.engine, cache.hit_rate, cache.kv_cache_used, cache.queried_tokens) == ("vllm", 0.42, 0.1, None)


def test_sglang_is_read_too():
    cache = parse_metrics(SGLANG)
    assert (cache.engine, cache.hit_rate, cache.kv_cache_used) == ("sglang", 0.66, 0.5)


def test_no_queries_yet_is_no_rate_not_zero():
    text = 'vllm:prefix_cache_queries_total{engine="0"} 0\nvllm:prefix_cache_hits_total{engine="0"} 0\n'
    assert parse_metrics(text).hit_rate is None


def test_metrics_with_no_cache_series_say_nothing():
    assert parse_metrics("process_cpu_seconds_total 12\n") is None


def test_several_engines_are_summed():
    text = (
        'vllm:prefix_cache_queries_total{engine="0"} 100\nvllm:prefix_cache_hits_total{engine="0"} 90\n'
        'vllm:prefix_cache_queries_total{engine="1"} 100\nvllm:prefix_cache_hits_total{engine="1"} 10\n'
    )
    assert parse_metrics(text).hit_rate == pytest.approx(0.5)


def test_the_rendered_vllm_unit_states_prefix_caching(tmp_path, monkeypatch):
    from axiom.extensions.builtins.llm_serving import cli

    monkeypatch.setattr(cli, "CFG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(cli, "UNIT_DIR", tmp_path / "units")
    (tmp_path / "cfg").mkdir()
    (tmp_path / "units").mkdir()
    monkeypatch.setattr(cli, "_systemctl", lambda *a: None)
    cli.cmd_install(cli.load_config(), None)
    unit = (tmp_path / "units" / "axiom-vllm.service").read_text()
    assert "--enable-prefix-caching" in unit
