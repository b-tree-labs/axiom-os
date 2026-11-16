# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A frame pulled with kit-pull gets its own gold object, verb and chart in one step.

Rehearsing Part 2 as a bound colleague: kit-pull fetched real channels, but kit-try kept
running the kit's invented example over invented channels, because nothing tied the
pulled channels to a gold object. ``kit-scaffold starter --from-snapshot`` writes the
three pieces over the pulled channels, filled in enough to pass kit-check.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.data_platform import kit
from axiom.extensions.builtins.data_platform.kit.project import KitError

ROWS = [
    {"site": "example-loop", "channel": ch, "ts": f"2026-10-01T15:0{m}:00Z", "value": 20.0 + m, "unit": "degC"}
    for ch in ("TC1", "TC2") for m in range(3)
]


def _kit_with_a_pull(tmp_path):
    kit.init(tmp_path / "site", "example-loop")
    p = kit.load(tmp_path / "site")
    kit.take_snapshot(p, site="example-loop", channels=["TC1", "TC2"], rows=ROWS,
                      window={"start": "2026-10-01T15:00:00Z", "end": "2026-10-01T16:00:00Z"},
                      snapshot_id="oct1-hour")
    return p


def test_the_starter_is_written_over_the_pulled_channels(tmp_path):
    p = _kit_with_a_pull(tmp_path)
    written = kit.scaffold_contribution(p, "starter", "loop_temps", from_snapshot="oct1-hour")
    assert sorted(written) == sorted([
        "gold/loop_temps.sql", "gold/loop_temps.toml", "verbs/loop_temps_by_hour.toml", "charts/loop_temps.json",
    ])
    sql = (p.root / "gold" / "loop_temps.sql").read_text()
    assert "channel IN ('TC1', 'TC2')" in sql
    chart = json.loads((p.root / "charts" / "loop_temps.json").read_text())
    assert chart["channels"] == ["TC1", "TC2"] and chart["window"]["from"].startswith("2026-10-01T15")


def test_the_starter_passes_the_declaration_checks(tmp_path):
    p = _kit_with_a_pull(tmp_path)
    kit.scaffold_contribution(p, "starter", "loop_temps", from_snapshot="oct1-hour")
    report = kit.check_kit(kit.load(p.root), isolation=False)
    assert report["ok"] and report["declarations"] == [], report


def test_a_starter_needs_a_snapshot(tmp_path):
    kit.init(tmp_path / "site", "example-loop")
    with pytest.raises(KitError, match="--from-snapshot"):
        kit.scaffold_contribution(kit.load(tmp_path / "site"), "starter", "loop_temps")


def _recent(hours_ago: int = 2) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


FED = [{"site": "example-loop", "channel": ch, "feed": feed, "ts": _recent(), "value": 20.0, "unit": "degC"}
       for ch, feed in (("TC1", "loop"), ("TC2", "rig"))]


def test_the_starters_view_is_bounded_by_each_channels_feed_and_a_window(tmp_path):
    """Unbounded, the view read the pulled channels' whole history and the node's statement
    timeout cancelled every verb over it. Each channel is matched in the feed the snapshot
    pulled it from, and ``ts`` is bounded to a declared window (the last 7 days)."""
    from axiom.extensions.builtins.data_platform.kit import declarations as decl

    kit.init(tmp_path / "site", "example-loop")
    p = kit.load(tmp_path / "site")
    kit.take_snapshot(p, site="example-loop", channels=["TC1", "TC2"], rows=FED,
                      window={"start": _recent(3), "end": _recent(0)}, snapshot_id="fed")
    kit.scaffold_contribution(p, "starter", "loop_temps", from_snapshot="fed")
    sql = (p.root / "gold" / "loop_temps.sql").read_text()
    assert "WHERE ts >= now() - interval '7 days'" in sql and "widened" not in sql
    assert "(feed = 'loop' AND channel IN ('TC1'))" in sql and "(feed = 'rig' AND channel IN ('TC2'))" in sql
    assert "To widen it" in sql, "the file says how to widen the window"
    ds = decl.load(kit.load(p.root))
    assert not decl.sql_problems(sql, allowed_schemas={"silver", "gold", "gold_example_loop"})
    assert not [w for w in decl.time_bound_warnings(ds) if "loop_temps" in w]


def test_an_older_snapshot_widens_the_starters_window_to_cover_it_and_stays_bounded(tmp_path):
    p = _kit_with_a_pull(tmp_path)  # pulled 2026-10-01, more than 7 days before any run of this
    kit.scaffold_contribution(p, "starter", "loop_temps", from_snapshot="oct1-hour")
    sql = (p.root / "gold" / "loop_temps.sql").read_text()
    import re

    days = int(re.search(r"WHERE ts >= now\(\) - interval '(\d+) days'", sql).group(1))
    assert days > 7 and "widened so it covers the snapshot" in sql
    assert "No feed is known for TC1, TC2" in sql, "a snapshot without feeds says what to add"


def test_kit_check_warns_on_a_gold_view_with_no_time_bound(tmp_path):
    p = _kit_with_a_pull(tmp_path)
    (p.root / "gold" / "everything.sql").write_text(
        "-- ts >= now() in a comment is not a bound\nSELECT channel, avg(value) AS mean FROM silver.signals "
        "WHERE channel = 'ts > 1' GROUP BY channel\n")
    (p.root / "gold" / "everything.toml").write_text('description = "Every reading ever."\n')
    (p.root / "gold" / "bounded.sql").write_text(
        "SELECT channel, avg(value) AS mean FROM silver.signals s WHERE s.ts BETWEEN '2026-01-01' AND now() "
        "GROUP BY channel\n")
    (p.root / "gold" / "bounded.toml").write_text('description = "A bounded one."\n')
    report = kit.check_kit(kit.load(p.root), isolation=False)
    warned = [n for n in report["notes"] if "no predicate on ts" in n]
    assert any("gold/everything.sql" in n for n in warned), report["notes"]
    assert not any("gold/bounded.sql" in n for n in warned)
    assert report["ok"], "a warning, not a finding"
