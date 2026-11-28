# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A self-hosted engine's prompt (prefix) cache, read from its own metrics (#2212, RS-H11).

vLLM does not reliably fill ``prompt_tokens_details.cached_tokens`` per response, so a
model that reuses its prefix looks uncached to the gateway. The engine's Prometheus
endpoint has the truth: the hit rate since the engine started and how full its KV cache
is. A rate nobody measured (no queries yet) is ``None``, never 0 (ADR-272 D6).
"""

from __future__ import annotations

import re
import ssl
import urllib.request
from dataclasses import dataclass

_SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+([-+0-9.eEinfNa]+)")


@dataclass(frozen=True)
class EngineCache:
    engine: str  # "vllm" | "sglang"
    #: Share of prompt tokens served from the cache since the engine started; None when unmeasured.
    hit_rate: float | None
    #: Share of the KV cache in use now (0..1); None when the engine does not say.
    kv_cache_used: float | None
    #: Prompt tokens the cache was asked about since the engine started, when the engine counts them.
    queried_tokens: int | None = None


def _sums(text: str) -> dict[str, float]:
    """Each metric's samples summed across labels (several engines or replicas add up)."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE.match(line)
        if not m:
            continue
        try:
            value = float(m.group(2))
        except ValueError:
            continue
        out[m.group(1)] = out.get(m.group(1), 0.0) + value
    return out


def _mean(text: str, name: str) -> float | None:
    values = [float(m.group(2)) for line in text.splitlines() if (m := _SAMPLE.match(line)) and m.group(1) == name]
    return sum(values) / len(values) if values else None


def parse_metrics(text: str) -> EngineCache | None:
    """The cache an engine reports in Prometheus text, or None when it reports none."""
    s = _sums(text)
    if "vllm:prefix_cache_queries_total" in s:
        queries = s["vllm:prefix_cache_queries_total"]
        hits = s.get("vllm:prefix_cache_hits_total", 0.0)
        return EngineCache(
            engine="vllm",
            hit_rate=hits / queries if queries > 0 else None,
            kv_cache_used=_mean(text, "vllm:kv_cache_usage_perc"),
            queried_tokens=int(queries),
        )
    if "vllm:gpu_prefix_cache_hit_rate" in s:  # vLLM before the v1 engine
        return EngineCache(
            engine="vllm",
            hit_rate=_mean(text, "vllm:gpu_prefix_cache_hit_rate"),
            kv_cache_used=_mean(text, "vllm:gpu_cache_usage_perc"),
        )
    if "sglang:cache_hit_rate" in s:
        return EngineCache(
            engine="sglang",
            hit_rate=_mean(text, "sglang:cache_hit_rate"),
            kv_cache_used=_mean(text, "sglang:token_usage"),
        )
    return None


def read_engine_cache(metrics_url: str, *, timeout: float = 5.0) -> EngineCache | None:
    """Fetch and parse an engine's ``/metrics``. A self-signed loopback certificate is accepted."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(metrics_url, timeout=timeout, context=ctx) as resp:  # noqa: S310 - operator's URL
        return parse_metrics(resp.read().decode("utf-8", "replace"))


def describe(cache: EngineCache | None) -> str:
    """One line for an operator."""
    if cache is None:
        return "the engine reports no prefix-cache metrics"
    rate = "not measured yet" if cache.hit_rate is None else f"{cache.hit_rate:.1%} of prompt tokens from cache"
    tokens = f" ({cache.queried_tokens:,} queried)" if cache.queried_tokens else ""
    kv = "" if cache.kv_cache_used is None else f"; KV cache {cache.kv_cache_used:.0%} in use"
    return f"{cache.engine} prefix cache: {rate}{tokens} since the engine started{kv}"
