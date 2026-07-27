# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Register-time promotion-map validation (ADR-001 P3): a bad map is a register
error, not a runtime crash."""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.data_platform.skills.register import _validate_promotion_map

_GOOD = """
[promotion]
target = "gold.series"
natural_key = ["obs_date"]
source_precedence = ["db.v1", "csv.v1"]
[promotion.columns]
obs_date = "slot"
predicted = "m.pred"
"""

_BAD_SEMANTICS = """
[promotion]
target = "gold.series"
natural_key = ["obs_date"]
[promotion.columns]
predicted = "m.pred"
"""   # natural_key 'obs_date' not in columns

_BAD_TOML = "[promotion\ntarget = broken"


def test_good_map_validates_clean(tmp_path: Path):
    p = tmp_path / "map.toml"
    p.write_text(_GOOD)
    assert _validate_promotion_map(str(p)) == []


def test_semantic_error_is_reported(tmp_path: Path):
    p = tmp_path / "map.toml"
    p.write_text(_BAD_SEMANTICS)
    errs = _validate_promotion_map(str(p))
    assert any("natural_key column 'obs_date'" in e for e in errs)


def test_broken_toml_is_reported_not_raised(tmp_path: Path):
    p = tmp_path / "map.toml"
    p.write_text(_BAD_TOML)
    errs = _validate_promotion_map(str(p))
    assert errs and "not valid TOML" in errs[0]


def test_missing_map_is_reported(tmp_path: Path):
    errs = _validate_promotion_map(str(tmp_path / "nope.toml"))
    assert errs and "not found" in errs[0]
