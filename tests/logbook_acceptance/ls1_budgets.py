# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The scale tests' side of the LS-1 budgets (#1780): read ``ls1_budgets.toml``, time a call,
record what was measured.

A scale test times each door with :func:`p95_ms`, hands the numbers to :func:`record`, and asserts
:func:`over` is empty. With ``AXIOM_SCALE_REPORT`` set (the nightly job), :func:`record` merges the
numbers into that JSON file, which ``scripts/ls1_scale_report.py`` judges against the baselines.
"""

from __future__ import annotations

import json
import os
import statistics
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

PATH = Path(__file__).with_name("ls1_budgets.toml")
REPORT_ENV = "AXIOM_SCALE_REPORT"


def load() -> dict[str, Any]:
    return tomllib.loads(PATH.read_text(encoding="utf-8"))


BUDGETS = load()
SCALE_ROWS = int(os.environ.get("AXIOM_SCALE_ROWS", str(BUDGETS["scale_rows"])))
SMOKE_ROWS = int(BUDGETS["smoke_rows"])


def limit(key: str) -> float:
    return float(BUDGETS["budget"][key]["limit"])


def p95_ms(call: Callable[[], Any], runs: int = 20, check: Callable[[Any], None] | None = None) -> float:
    """The 95th percentile of ``runs`` timed calls, after one call that warms the plan."""
    check = check or (lambda r: None)
    check(call())
    samples = []
    for _ in range(runs):
        t0 = time.perf_counter()
        result = call()
        samples.append((time.perf_counter() - t0) * 1000)
        check(result)
    return statistics.quantiles(samples, n=20)[-1] if len(samples) >= 20 else max(samples)


def record(numbers: dict[str, float], *, rows: int) -> None:
    """Publish ``numbers`` (ms by budget key) into ``$AXIOM_SCALE_REPORT``, merged with what other
    tests of the run recorded. Unknown keys are refused, so a typo cannot hide a budget."""
    unknown = set(numbers) - set(BUDGETS["budget"])
    assert not unknown, f"not budgets in {PATH.name}: {sorted(unknown)}"
    print(f"LS-1 p95 ms at {rows} rows: { {k: round(v, 1) for k, v in numbers.items()} }")
    target = os.environ.get(REPORT_ENV)
    if not target:
        return
    path = Path(target)
    report = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    report["rows"] = rows
    report.setdefault("numbers", {}).update({k: round(float(v), 2) for k, v in numbers.items()})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


def over(numbers: dict[str, float]) -> dict[str, int]:
    """The enforced budgets ``numbers`` break (at or over the limit)."""
    out = {}
    for key, value in numbers.items():
        b = BUDGETS["budget"][key]
        if b.get("status") == "enforced" and value >= float(b["limit"]):
            out[key] = round(value)
    return out
