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
    assert "WHERE channel IN ('TC1', 'TC2')" in sql
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
