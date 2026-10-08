# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Portable governance-overhead measurement — ours next to everybody else's.

This platform can say what its gate, receipt and audit layers cost per action.
That is a useful internal number and an unfalsifiable external one: "8 ms of
governance" means nothing until you know what the alternatives charge for the
same guarantees, and the honest possibility is that we are expensive.

Generalising the benchmark is therefore not packaging work — it is what makes
the claim capable of coming out against us.

A **harness** is any agent framework that can run an action under whatever it
calls governance. It contributes named :class:`Variant` s, always including
``bare``, and the engine does the timing, the allocation pass, the percentiles
and the deltas. Adapters load by dotted path so measuring a competitor never
requires this package to import, or depend on, their framework.

Two properties carry the whole comparison and both are easy to lose:

**Every harness runs the IDENTICAL action body.** :func:`action_body` lives here
and is handed to adapters. If each brought its own, the benchmark would be
comparing the bodies.

**Seams that are not alike are not summed or ranked.** One framework's
"middleware hook" is not another's "policy guard plus signed receipt plus audit
append". Each variant declares the guarantees it buys, and a cross-harness
comparison refuses to rank variants whose declared guarantees differ — the
resulting number would be arithmetically fine and mean nothing.

Percentiles are nearest-rank, never interpolated: an interpolated percentile
invents a latency no iteration had.
"""

from __future__ import annotations

import math
import platform
import statistics
import time
import tracemalloc
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

#: The payload every harness's action body operates on. Small and boring on
#: purpose — the benchmark measures the governance around the work, so the work
#: must not dominate, vary, or differ between harnesses.
PAYLOAD: dict[str, Any] = {
    "candidate": "resource-42",
    "op": "bench.op",
    "attempt": 1,
}


def action_body(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """The work every harness governs. Identical across all of them.

    Deterministic and allocation-stable: build a small result dict and
    serialize it. A harness that supplied its own body would make the benchmark
    a comparison of bodies, which is the easiest way for this measurement to
    become meaningless without looking wrong.
    """
    import json

    source = payload if payload is not None else PAYLOAD
    result = {
        "candidate": source["candidate"],
        "op": source["op"],
        "outcome": "ok",
    }
    return {"serialized": json.dumps(result, sort_keys=True), **result}


@dataclass(frozen=True)
class Variant:
    """One measured configuration of a harness.

    ``guarantees`` is what this seam actually buys — ``("audit",)``,
    ``("approval", "receipt")``, and so on. It is not decoration: it is what
    lets a cross-harness comparison refuse to rank a middleware hook against a
    signed receipt.
    """

    name: str
    run: Callable[[], Any]
    guarantees: tuple[str, ...] = ()
    #: Optional per-iteration reset, run OUTSIDE the timed region (re-seeding a
    #: queue, truncating a file) so a growing structure does not masquerade as
    #: rising overhead.
    reset: Callable[[], None] | None = None
    description: str = ""


@dataclass
class Harness:
    """A framework under measurement. Must offer a ``bare`` variant."""

    name: str
    variants: list[Variant] = field(default_factory=list)
    version: str = ""
    notes: str = ""

    def variant(self, name: str) -> Variant | None:
        return next((v for v in self.variants if v.name == name), None)


#: The agreed name for the "everything on" path. Harnesses are only comparable
#: where their variant names line up, so the composed seam has one name across
#: adapters. Without it every adapter invents its own and a cross-harness
#: comparison is empty by construction — which it silently was, until a real run
#: produced a report with nothing in it.
COMPOSED_VARIANT = "governed_full"

#: Labels every result carries. A benchmark that travels without them gets
#: quoted as though it were a population estimate.
BASE_HONESTY_LABELS: tuple[str, ...] = (
    "single machine",
    "single process",
    "one run",
    "percentiles are nearest-rank, not interpolated",
)


def _percentiles(samples_ns: Sequence[int]) -> dict[str, float]:
    ordered = sorted(samples_ns)
    n = len(ordered)

    def at(fraction: float) -> float:
        index = max(0, math.ceil(fraction * n) - 1)
        return round(ordered[index] / 1000.0, 2)

    return {
        "p50_us": at(0.50),
        "p95_us": at(0.95),
        "p99_us": at(0.99),
        "mean_us": round(statistics.fmean(ordered) / 1000.0, 2),
    }


def _time_variant(variant: Variant, *, iterations: int, warmup: int) -> dict[str, float]:
    for _ in range(max(0, warmup)):
        if variant.reset:
            variant.reset()
        variant.run()

    samples: list[int] = []
    for _ in range(max(1, iterations)):
        # Reset is OUTSIDE the timed region on purpose: a queue that grows for
        # the whole run would otherwise read as governance getting slower.
        if variant.reset:
            variant.reset()
        start = time.perf_counter_ns()
        variant.run()
        samples.append(time.perf_counter_ns() - start)
    return _percentiles(samples)


def _alloc_variant(variant: Variant, *, iterations: int) -> float:
    """Median per-iteration traced-memory peak, in KiB.

    A separate pass: tracemalloc slows execution enough to distort timing, so
    it never shares a run with it.
    """
    peaks: list[float] = []
    tracemalloc.start()
    try:
        for _ in range(max(1, iterations)):
            if variant.reset:
                variant.reset()
            tracemalloc.reset_peak()
            variant.run()
            _, peak = tracemalloc.get_traced_memory()
            peaks.append(peak / 1024.0)
    finally:
        tracemalloc.stop()
    return round(statistics.median(peaks), 2)


def measure(
    harness: Harness,
    *,
    iterations: int = 1000,
    warmup: int = 50,
    alloc_iterations: int = 200,
) -> dict[str, Any]:
    """Time every variant and report deltas against ``bare``.

    A harness with no ``bare`` variant is refused rather than reported in
    absolute terms: without the shared baseline there is nothing to subtract,
    and absolute numbers from different machines look comparable and are not.
    """
    if harness.variant("bare") is None:
        raise ValueError(
            f"harness {harness.name!r} has no 'bare' variant; there is nothing "
            "to take a delta against, and absolute timings across harnesses are "
            "not comparable"
        )

    timings: dict[str, dict[str, float]] = {}
    allocs: dict[str, float] = {}
    for variant in harness.variants:
        timings[variant.name] = _time_variant(
            variant, iterations=iterations, warmup=warmup
        )
        allocs[variant.name] = _alloc_variant(variant, iterations=alloc_iterations)

    bare, bare_alloc = timings["bare"], allocs["bare"]
    deltas = {
        name: {
            "p50_us": round(t["p50_us"] - bare["p50_us"], 2),
            "p95_us": round(t["p95_us"] - bare["p95_us"], 2),
            "p99_us": round(t["p99_us"] - bare["p99_us"], 2),
            "mean_us": round(t["mean_us"] - bare["mean_us"], 2),
            "alloc_peak_median_kib": round(allocs[name] - bare_alloc, 2),
        }
        for name, t in timings.items()
        if name != "bare"
    }

    return {
        "harness": harness.name,
        "harness_version": harness.version,
        "notes": harness.notes,
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "machine": platform.machine(),
        },
        "methodology": {
            "iterations": iterations,
            "warmup_discarded": warmup,
            "alloc_iterations": alloc_iterations,
            "action_body": "axiom.evals.overhead.action_body (shared by every harness)",
            "honesty_labels": list(BASE_HONESTY_LABELS),
        },
        "guarantees": {v.name: list(v.guarantees) for v in harness.variants},
        "variants": {**timings, **{}},
        "alloc_peak_median_kib": allocs,
        "deltas_vs_bare": deltas,
    }


def compare(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Put harness results side by side, ranking ONLY where that is honest.

    A variant present in two harnesses is ranked only when both declare the same
    guarantees. Otherwise the row is marked not comparable and says why — a
    middleware hook timed against a guard plus signed receipt plus audit append
    produces a perfectly valid subtraction and a worthless conclusion.
    """
    results = list(results)
    by_variant: dict[str, Any] = {}
    variant_names = {
        name
        for result in results
        for name in result["deltas_vs_bare"]
    }

    for name in sorted(variant_names):
        present = [r for r in results if name in r["deltas_vs_bare"]]
        if len(present) < 2:
            continue
        guarantee_sets = {
            tuple(sorted(r["guarantees"].get(name, []))) for r in present
        }
        row: dict[str, Any] = {
            "harnesses": {
                r["harness"]: r["deltas_vs_bare"][name]["p50_us"] for r in present
            },
            "guarantees": {
                r["harness"]: r["guarantees"].get(name, []) for r in present
            },
        }
        if len(guarantee_sets) == 1:
            row["comparable"] = True
            row["note"] = ""
            row["cheapest"] = min(row["harnesses"], key=lambda h: row["harnesses"][h])
        else:
            row["comparable"] = False
            row["cheapest"] = None
            row["note"] = (
                "not ranked: these harnesses declare DIFFERENT guarantees for "
                "this variant, so the cheaper number may simply be buying less"
            )
        by_variant[name] = row

    # An empty comparison reads exactly like one that found no difference, so
    # say when there was nothing to compare at all.
    note = ""
    if len(results) > 1 and not by_variant:
        note = (
            "no variant name is shared by these harnesses, so nothing was "
            f"compared. Name the composed path {COMPOSED_VARIANT!r} in each "
            "adapter to make them line up."
        )

    return {
        "harnesses": [r["harness"] for r in results],
        "by_variant": by_variant,
        "comparable_variants": sorted(by_variant),
        "note": note,
        "results": results,
        "honesty_labels": list(BASE_HONESTY_LABELS)
        + ["cross-harness rows are only ranked when declared guarantees match"],
    }


def load_adapter(path: str) -> Callable[..., Harness]:
    """Load a harness builder from ``package.module:function``.

    Dotted-path loading is what keeps a competitor's framework out of this
    package's dependencies: an adapter for another harness lives wherever that
    framework is installed, and this never imports it unless asked.
    """
    import importlib

    module_path, _, attr = path.partition(":")
    if not module_path or not attr:
        raise ValueError(
            f"adapter path {path!r} must be 'package.module:function'"
        )
    try:
        module = importlib.import_module(module_path)
    except Exception as exc:  # noqa: BLE001 — the path is the useful detail
        raise ValueError(f"cannot import adapter {module_path!r}: {exc}") from exc
    builder = getattr(module, attr, None)
    if not callable(builder):
        raise ValueError(f"adapter {path!r} has no callable {attr!r}")
    return builder


__all__ = [
    "BASE_HONESTY_LABELS",
    "COMPOSED_VARIANT",
    "Harness",
    "PAYLOAD",
    "Variant",
    "action_body",
    "compare",
    "load_adapter",
    "measure",
]
