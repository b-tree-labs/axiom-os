# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A kit starts from a CSV the platform has never seen.

`kit-scaffold conform <name> --from-csv <file>` learns the time column and the
numeric channels from the file, writes a working reader and declares the source.
Units stay blank on purpose: kit-check fails a channel with no unit, so a guess
never lands as a fact. The integration test fills them in and runs the loop.
"""

from __future__ import annotations

import os
import shutil

import pytest

from axiom.extensions.builtins.data_platform import kit
from axiom.extensions.builtins.data_platform.kit.contribute import csv_shape
from axiom.extensions.builtins.data_platform.kit.project import KitError

CSV = (
    "Timestamp,Flow (L/min),Inlet Temp C,Operator,Valve %\n"
    "2026-09-01T00:00:00Z,12.5,21.0,ann,40\n"
    "2026-09-01T00:01:00Z,12.7,21.2,ann,41\n"
    "2026-09-01T00:02:00Z,,21.3,bob,41\n"
    "2026-09-01T00:03:00Z,12.9,21.5,bob,42\n"
)


def test_the_shape_is_learned_from_the_file(tmp_path):
    f = tmp_path / "loop.csv"
    f.write_text(CSV)
    time_col, nums = csv_shape(f)
    assert time_col == "Timestamp"
    assert nums == ["Flow (L/min)", "Inlet Temp C", "Valve %"], "text columns are not channels"


def test_a_file_with_no_times_is_refused_with_the_fix(tmp_path):
    f = tmp_path / "bad.csv"
    f.write_text("a,b\n1,2\n")
    with pytest.raises(KitError, match="no column holds ISO times"):
        csv_shape(f)


def test_the_scaffold_writes_a_reader_and_declares_the_source(tmp_path):
    f = tmp_path / "loop.csv"
    f.write_text(CSV)
    kit.init(tmp_path / "site", "example-loop")
    p = kit.load(tmp_path / "site")
    written = kit.scaffold_contribution(p, "conform", "loop_csv", from_csv=str(f))
    reader = (p.root / "conform" / "loop_csv.py").read_text()
    assert "TIME_COLUMN = 'Timestamp'" in reader and "'Flow (L/min)': ('flow_l_min', \"\")" in reader
    assert '[sources.loop_csv]' in (p.root / "kit.toml").read_text()
    assert any(w.startswith("kit.toml") for w in written)


@pytest.mark.integration
@pytest.mark.skipif(not (shutil.which("docker") or os.environ.get("AXIOM_KIT_DSN")),
                    reason="needs Docker or AXIOM_KIT_DSN")
def test_the_csv_reaches_silver_once_units_are_filled_in(tmp_path):
    f = tmp_path / "loop.csv"
    f.write_text(CSV)
    repo = tmp_path / "site"
    kit.init(repo, "example-loop")
    p = kit.load(repo)
    kit.scaffold_contribution(p, "conform", "loop_csv", from_csv=str(f))
    reader = p.root / "conform" / "loop_csv.py"
    text = reader.read_text()
    for channel, unit in (("flow_l_min", "L/min"), ("inlet_temp_c", "degC"), ("valve", "%")):
        text = text.replace(f"('{channel}', \"\")", f"('{channel}', {unit!r})")  # the contributor fills these in
    reader.write_text(text)
    p = kit.load(repo)
    import logging

    from axiom.extensions.builtins.data_platform.skills import kit as kit_skills
    from axiom.infra.skills import SkillContext, SkillRegistry

    ctx = SkillContext(registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("t"))
    r = kit_skills.kit_sample({"dir": str(repo), "source": "loop_csv", "origin": str(f)}, ctx)
    assert r.ok, r.errors
    kit.up(p)
    try:
        out = kit.try_kit(kit.load(repo))
        assert out["silver"]["errored"] == 0
        from axiom.extensions.builtins.data_platform.kit.medallion import connect, dsn
        with connect(dsn(p)) as conn:
            got = dict(conn.execute(
                "SELECT channel, count(*) FROM silver.signals WHERE feed = 'loop_csv' GROUP BY channel").fetchall())
        assert got == {"flow_l_min": 3, "inlet_temp_c": 4, "valve": 4}, got
    finally:
        kit.down(p)
