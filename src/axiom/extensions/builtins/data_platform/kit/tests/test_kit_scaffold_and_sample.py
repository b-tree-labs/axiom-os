# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""kit-scaffold and kit-sample, with nothing installed.

A scaffold is judged by what kit-check says about it next: a chart or a
normalizer that is valid as written, or a gold object and a verb that fail
the check by naming exactly what is left to fill in. Never a file that looks
finished and is not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform import kit
from axiom.extensions.builtins.data_platform.kit import declarations as decl
from axiom.extensions.builtins.data_platform.kit.project import KitError
from axiom.extensions.builtins.data_platform.kit.runner import load_normalizers


@pytest.fixture
def project(tmp_path: Path):
    kit.init(tmp_path, "rig-site")
    return kit.load(tmp_path)


def test_the_inventory_reads_the_samples_through_the_kits_own_normalizers(project):
    inv = kit.inventory(project)
    assert set(inv["channels"]) == {"temperature.inlet", "temperature.outlet", "flow.primary"}
    assert inv["channels"]["temperature.inlet"]["unit"] == "degC"
    assert inv["first"].isoformat() == "2026-09-01T00:00:00+00:00"


def test_a_scaffolded_chart_is_a_valid_chartspec_over_real_channels(project):
    (rel,) = kit.scaffold_contribution(project, "chart", "inlet_outlet")
    doc = json.loads((project.root / rel).read_text())
    assert doc["schema_version"] == "1.1" and doc["site"] == "rig-site"
    assert doc["channels"] == ["temperature.inlet", "temperature.outlet"], (
        "the most common declared unit's channels: one unit, one axis"
    )
    assert doc["window"]["from"] == "2026-09-01T00:00:00+00:00"
    assert doc["transform"] == {"bucket": "1h", "agg": "mean"}
    problems = [p for p in decl.load(project).problems if "inlet_outlet" in p]
    assert problems == []


def test_a_scaffolded_normalizer_loads_and_registers(project):
    kit.scaffold_contribution(project, "conform", "second_rig")
    kit.scaffold_contribution(project, "conform", "archive", deposit=True)
    registry, deposits, loaded = load_normalizers(project)
    assert "rig-site/second_rig-v1" in registry.refs()
    assert "rig-site/archive-v1" in deposits.refs()
    assert {"second_rig.py", "archive.py"} <= set(loaded)
    assert (project.root / "conform" / "tests" / "test_second_rig.py").is_file()


def test_gold_and_verb_scaffolds_fail_the_check_naming_what_is_left(project):
    kit.scaffold_contribution(project, "gold", "hourly")
    kit.scaffold_contribution(project, "verb", "hourly_question")
    problems = decl.load(project).problems
    assert any("gold/hourly.sql" in p and "description" in p for p in problems)
    assert any("verbs/hourly_question.toml" in p and "description" in p for p in problems)


def test_a_scaffolded_derived_channel_uses_two_channels_of_one_unit(project):
    kit.scaffold_contribution(project, "derived", "outlet_minus_inlet")
    ds = decl.load(project)
    (dv,) = [d for d in ds.derived if d.name == "outlet_minus_inlet"]
    assert dv.expression == "temperature.outlet - temperature.inlet" and dv.unit == "degC"


@pytest.mark.parametrize("kind", ["chart", "gold", "verb", "conform"])
def test_kit_scaffold_never_overwrites(project, kind):
    kit.scaffold_contribution(project, kind, "twice")
    with pytest.raises(KitError, match="already exists"):
        kit.scaffold_contribution(project, kind, "twice")


@pytest.mark.parametrize(("kind", "name"), [("table", "x"), ("chart", "Bad-Name"), ("gold", "")])
def test_an_unknown_kind_or_a_bad_name_is_refused(project, kind, name):
    with pytest.raises(KitError):
        kit.scaffold_contribution(project, kind, name)


def test_sample_brings_rows_and_reports_what_the_normalizers_read(project, tmp_path):
    src = tmp_path / "mine"
    src.mkdir()
    (src / "day.jsonl").write_text(
        json.dumps(
            {
                "schema_ref": "example/rig-frame-v1",
                "row_hash": "mine-1",
                "row": {"ts": "2026-10-01T00:00:00+00:00", "t_in": 30.0},
            }
        )
        + "\n"
    )
    out = kit.sample(project, "rig-a", src)
    assert out["added"] == ["day.jsonl"]
    assert out["last"] == "2026-10-01T00:00:00+00:00", "the new day now ends the samples"


def test_sample_refuses_a_non_jsonl_file_for_a_rows_source(project, tmp_path):
    f = tmp_path / "run.tdms"
    f.write_bytes(b"x")
    with pytest.raises(KitError, match='kind = "deposit"'):
        kit.sample(project, "rig-a", f)


def test_sample_refuses_an_undeclared_source(project, tmp_path):
    f = tmp_path / "a.jsonl"
    f.write_text("{}\n")
    with pytest.raises(KitError, match="no source 'nope'"):
        kit.sample(project, "nope", f)


def test_sample_content_addresses_deposits_and_keeps_their_folders(tmp_path):
    kit.init(tmp_path / "repo", "rig-site")
    manifest = tmp_path / "repo" / "data" / "kit.toml"
    manifest.write_text(
        manifest.read_text()
        + '\n[sources.share]\nkind = "deposit"\nschema_ref = "rig-site/archive-v1"\n'
    )
    project = kit.load(tmp_path / "repo")
    kit.scaffold_contribution(project, "conform", "archive", deposit=True)
    share = tmp_path / "share" / "run-1"
    share.mkdir(parents=True)
    (share / "samples.csv").write_text("t,v\n")
    (share / "channels.csv").write_text("id,name\n")
    out = kit.sample(project, "share", tmp_path / "share")
    assert sorted(out["added"]) == ["run-1/channels.csv", "run-1/samples.csv"]
    from axiom.extensions.builtins.data_platform.conformance.deposits import load_deposits

    loaded, missing, _ = load_deposits(project.folder("samples") / "bronze", "share")
    assert missing == []
    (samples,) = [d for d in loaded if d.name == "samples.csv"]
    assert samples.companion(lambda d: d.name == "channels.csv") is not None, (
        "files that arrive together stay companions"
    )
    assert any("samples.csv" in u and "NotImplementedError" in u for u in out["unreadable"]), (
        "a scaffold that is not written yet is reported, not a crash"
    )


def test_the_verbs_declare_what_they_need_and_say_why_when_it_is_missing(
    project, tmp_path, tmp_path_factory
):
    from axiom.extensions.builtins.data_platform.skills import bind_default

    registry = bind_default()
    for verb in ("data.kit_scaffold", "data.kit_sample"):
        spec = registry.spec(verb)
        assert spec.risk == "reversible-write", "only adds files and never overwrites one"
    sample = {p.id: p for p in registry.spec("data.kit_sample").requires}
    ok = {"dir": str(project.root.parent), "source": "rig-a", "origin": str(project.root)}
    for name in ("kit-present", "source-declared", "origin-exists"):
        assert sample[name].check(ok, None) == (True, "")
    met, why = sample["source-declared"].check({**ok, "source": "nope"}, None)
    assert not met and "rig-a" in why, "names what IS declared"
    # Its own tree: the loader walks UP to find data/kit.toml, so a folder
    # beside the fixture's kit would correctly find that kit.
    bare = tmp_path_factory.mktemp("no-kit-here")
    met, why = sample["kit-present"].check({"dir": str(bare)}, None)
    assert not met and "kit-init" in why
    met, _ = sample["origin-exists"].check({**ok, "origin": str(tmp_path / "missing")}, None)
    assert not met


def test_kit_inits_own_gold_example_passes_the_time_bound_check_it_ships_with(project):
    """Found live: a fresh kit-init's kit-check warned about its own gold/daily_stats.sql having no
    predicate on ts. The example now reads a recent window, wide enough to reach the samples."""
    from datetime import date

    from axiom.extensions.builtins.data_platform.kit import scaffold

    assert decl.time_bound_warnings(decl.load(project)) == []
    sql = (project.folder("gold") / "daily_stats.sql").read_text()
    assert "{{" not in sql
    assert "WHERE ts >= now() - interval '" in sql
    # 7 days at least, and back to the oldest sample day so kit-try shows rows.
    assert scaffold.example_window_days(date(2026, 9, 3)) == scaffold.EXAMPLE_WINDOW_DAYS
    assert scaffold.example_window_days(date(2026, 10, 27)) == 57
