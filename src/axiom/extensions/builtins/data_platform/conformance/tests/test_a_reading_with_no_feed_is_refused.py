# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A reading with an empty feed is refused, counted and loud, never written.

A normalizer read ``feed`` from frames that still carried the pre-rename
``stream`` key and returned ``feed = ''``. A rebuild wrote 27.3M such readings
and reported success; the feed is part of the reading identity, so every chart
and gold verb asking for the real feed found nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

from axiom.extensions.builtins.data_platform.conformance import NormalizerRegistry, conform_rows
from axiom.extensions.builtins.data_platform.conformance.runner import conform_verdict


def _land(root: Path) -> None:
    day = root / "site-a-live" / "_rows" / "2026-10-08"
    day.mkdir(parents=True)
    rec = {"schema_ref": "s/v1", "row_hash": "h1", "row": {"ts": "2026-10-08T12:00:00Z"}}
    (day / "item-1.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _run(tmp_path, feed):
    _land(tmp_path)
    reg = NormalizerRegistry()
    reg.register(
        "s/v1",
        lambda rec: [
            {"feed": feed, "channel": "dT", "ts": rec["row"]["ts"], "value": 1.0},
            {"feed": "example.loop", "channel": "dT", "ts": rec["row"]["ts"], "value": 1.0},
        ],
    )
    written: list[dict] = []
    stats = conform_rows(tmp_path, reg, {"site-a-live": "site-a"}, upsert=written.append)
    return stats, written


def test_an_empty_feed_is_not_written(tmp_path):
    stats, written = _run(tmp_path, "")
    assert [r["feed"] for r in written] == ["example.loop"]
    assert stats["rows_out"] == 1
    assert stats["missing_identity"] == {"s/v1 (no feed)": 1}


def test_a_strict_run_fails_and_says_why(tmp_path):
    stats, _ = _run(tmp_path, "  ")
    verdict = conform_verdict(stats, strict=True)
    assert not verdict.ok
    assert any("empty identity field" in m and "s/v1 (no feed)×1" in m for m in verdict.messages)


def test_a_clean_pass_is_unchanged(tmp_path):
    stats, written = _run(tmp_path, "example.other")
    assert len(written) == 2 and "missing_identity" not in stats
    assert conform_verdict(stats, strict=True).ok
