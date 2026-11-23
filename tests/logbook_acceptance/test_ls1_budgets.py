# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LS-1 (#1780): the budgets are data, and the nightly verdict fails on a 20% regression.

- LB-LS1-A5: one budgets file names every budget the issue lists, each with a limit, a status
  and either the test that measures it or what it needs; the records door's field filter
  (ADR-268 LF-A24) has its row, enforced since phase 2 of the field filters landed it;
- LB-LS1-A6: the report fails an enforced number over its limit, a number more than 20% (and
  more than the noise floor) over its stored baseline, and an enforced budget with no number;
  it passes a first run with no baseline, lists pending rows without failing, and writes the
  measured numbers back as baselines on request.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from . import ls1_budgets

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("ls1_scale_report", ROOT / "scripts" / "ls1_scale_report.py")
report = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("ls1_scale_report", report)
_spec.loader.exec_module(report)

ISSUE_BUDGETS = {
    "log_first_page": 200, "timeline_first": 200, "log_search": 300, "page_first_paint": 1000,
    "page_scroll_frame": 50, "tail_frame_at_50_per_s": 50, "export_100k_first_bytes": 1000,
    "verify_slice_10k": 2000, "field_filter_first_page": 200,
}  # fmt: skip


@pytest.mark.acceptance("LB-LS1-A5")
def test_lb_ls1_a5_one_file_holds_every_budget():
    b = ls1_budgets.BUDGETS
    assert b["regression"] == 0.20 and b["scale_rows"] == 1_000_000 and b["smoke_rows"] == 10_000
    for key, ms in ISSUE_BUDGETS.items():
        assert b["budget"][key]["limit"] == ms, key
    for key, row in b["budget"].items():
        assert row["status"] in ("enforced", "tracked", "pending"), key
        assert row.get("what"), key
        if row["status"] == "pending":
            assert row.get("needs"), f"{key}: a pending budget says what lands it"
        else:
            assert (ROOT / row["test"]).is_file(), f"{key}: {row['test']}"
            if row["status"] == "enforced":
                assert row.get("limit"), key
    lf = b["budget"]["field_filter_first_page"]
    assert "LF-A24" in lf["what"] and lf["limit"] == b["budget"]["log_first_page"]["limit"]
    assert lf["status"] == "enforced" and lf["test"].endswith("test_lf2.py")


def _budgets(**rows):
    return {"regression": 0.20, "noise_ms": 5.0, "budget": rows}


@pytest.mark.acceptance("LB-LS1-A6")
def test_lb_ls1_a6_the_verdict_fails_over_budget_on_regression_and_on_a_missing_number():
    b = _budgets(
        a={"what": "a", "limit": 200.0, "baseline": 100.0, "status": "enforced"},
        b={"what": "b", "limit": 200.0, "baseline": 100.0, "status": "enforced"},
        c={"what": "c", "limit": 200.0, "baseline": 0.0, "status": "enforced"},
        d={"what": "d", "limit": 200.0, "baseline": 3.0, "status": "enforced"},
        e={"what": "e", "baseline": 100.0, "status": "tracked"},
        f={"what": "f", "limit": 1000.0, "status": "pending", "needs": "x"},
        g={"what": "g", "limit": 200.0, "baseline": 0.0, "status": "enforced"},
    )
    got = {v.key: v.verdict for v in report.evaluate(
        b, {"a": 119.0, "b": 121.0, "c": 150.0, "d": 7.9, "e": 130.0, "g": 200.0})}  # fmt: skip
    assert got == {"a": "ok", "b": "regressed", "c": "no-baseline", "d": "ok",
                   "e": "regressed", "f": "pending", "g": "over"}  # fmt: skip
    missing = report.evaluate(b, {})
    assert {v.key for v in missing if v.verdict == "missing"} == {"a", "b", "c", "d", "g"}


@pytest.mark.acceptance("LB-LS1-A6")
def test_lb_ls1_a6_the_report_publishes_a_summary_and_exits_by_its_verdict(tmp_path, capsys):
    numbers = {k: 1.0 for k, row in ls1_budgets.BUDGETS["budget"].items() if row["status"] != "pending"}
    path = tmp_path / "numbers.json"
    path.write_text(json.dumps({"rows": 1_000_000, "numbers": numbers}))
    summary = tmp_path / "summary.md"
    assert report.main(["--numbers", str(path), "--summary", str(summary)]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith("LS-1 at 1,000,000 entries:") and "within budget" in first
    assert "| `log_first_page` |" in summary.read_text() and "pending" in summary.read_text()
    numbers["log_first_page"] = 250.0
    path.write_text(json.dumps({"rows": 1_000_000, "numbers": numbers}))
    assert report.main(["--numbers", str(path)]) == 1
    assert "log_first_page over" in capsys.readouterr().out
    assert report.main(["--numbers", str(tmp_path / "absent.json")]) == 1


@pytest.mark.acceptance("LB-LS1-A6")
def test_lb_ls1_a6_measured_numbers_become_the_stored_baseline_on_request(tmp_path):
    copy = tmp_path / "budgets.toml"
    copy.write_text(ls1_budgets.PATH.read_text())
    nums = tmp_path / "n.json"
    nums.write_text(json.dumps({"rows": 10, "numbers": {"log_first_page": 12.34, "tail_forward": 5}}))
    assert report.main(["--numbers", str(nums), "--budgets", str(copy), "--update-baseline"]) == 0
    stored = report.load_budgets(copy)["budget"]
    assert stored["log_first_page"]["baseline"] == 12.3 and stored["tail_forward"]["baseline"] == 5.0
    assert stored["log_older_page"]["baseline"] == 0.0


def test_recording_merges_into_the_run_report_and_refuses_an_unknown_key(tmp_path, monkeypatch):
    target = tmp_path / "out" / "numbers.json"
    monkeypatch.setenv(ls1_budgets.REPORT_ENV, str(target))
    ls1_budgets.record({"log_first_page": 10.0}, rows=100)
    ls1_budgets.record({"timeline_first": 20.0}, rows=100)
    assert json.loads(target.read_text())["numbers"] == {"log_first_page": 10.0, "timeline_first": 20.0}
    with pytest.raises(AssertionError, match="not budgets"):
        ls1_budgets.record({"log_frist_page": 1.0}, rows=100)
    assert ls1_budgets.over({"log_first_page": 200.0, "export_100k_build": 9e9}) == {"log_first_page": 200}
