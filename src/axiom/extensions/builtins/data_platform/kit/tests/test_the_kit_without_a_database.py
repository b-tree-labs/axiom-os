"""The tenant data kit, everything that needs no database.

A provider's first minute is `kit-init` and their CI's first job may be
`kit-check --static-only` on a runner with no Docker, so both have to work and
say useful things with nothing installed.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform import kit
from axiom.extensions.builtins.data_platform.kit import declarations as decl
from axiom.extensions.builtins.data_platform.kit.check import check_kit, normalizer_findings
from axiom.extensions.builtins.data_platform.kit.project import KitError
from axiom.extensions.builtins.data_platform.kit.scaffold import TEMPLATE


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    kit.init(tmp_path, "rig-site")
    return tmp_path


def test_init_writes_one_example_of_each_kind_with_the_tenant_filled_in(repo):
    data = repo / "data"
    for rel in (
        "kit.toml",
        "conform/rig_frame.py",
        "silver/derived.toml",
        "silver/roles.toml",
        "gold/daily_stats.sql",
        "gold/daily_stats.toml",
        "verbs/temperature_rise.toml",
        "charts/daily_overview.json",
        "notebooks/first-contribution.py",
        "README.md",
        "AGENTS.md",
    ):
        assert (data / rel).is_file(), rel
    assert 'tenant = "rig-site"' in (data / "kit.toml").read_text()
    assert "{{" not in (data / "kit.toml").read_text()
    assert list((data / "samples" / "bronze").rglob("*.jsonl")), (
        "the examples must run on a fresh clone"
    )


def test_init_refuses_to_overwrite_a_contribution(repo):
    with pytest.raises(KitError, match="already has files"):
        kit.init(repo, "rig-site")


@pytest.mark.parametrize("bad", ["", "RIG", "rig_site", "1rig", "a" * 60, "rig;drop"])
def test_a_tenant_that_cannot_be_a_schema_name_is_refused(tmp_path, bad):
    with pytest.raises(KitError):
        kit.init(tmp_path, bad)


def test_the_examples_declare_cleanly(repo):
    ds = decl.load(kit.load(repo))
    assert ds.problems == []
    assert [d.name for d in ds.derived] == ["temperature.rise"]
    assert [g.name for g in ds.gold] == ["daily_stats"]
    assert [v.obj for v in ds.verbs] == ["daily_stats"]


def test_the_example_normalizer_is_clean_and_states_its_uncertainty_posture(repo):
    blocking, notes = normalizer_findings(kit.load(repo))
    assert blocking == []
    assert len(notes) == 1 and "uncharacterised" in notes[0]


def test_without_a_posture_the_undeclared_uncertainty_is_named_per_channel(repo):
    toml = repo / "data" / "kit.toml"
    toml.write_text(toml.read_text().split("[uncertainty]")[0])
    _, notes = normalizer_findings(kit.load(repo))
    assert len(notes) == 3 and all("declare no uncertainty" in n for n in notes)


@pytest.mark.parametrize(
    "sql, why",
    [
        ("DELETE FROM silver.signals", "only reads"),
        ("SELECT 1; SELECT 2", "more than one statement"),
        ("SELECT * FROM public.users", "reads public.users"),
        ("SELECT * FROM gold_other_tenant.daily", "reads gold_other_tenant.daily"),
        ("CREATE TABLE x AS SELECT 1", "must start with SELECT"),
    ],
)
def test_a_gold_object_is_one_read_of_permitted_relations(sql, why):
    problems = decl.sql_problems(sql, allowed_schemas={"silver", "gold", "gold_rig_site"})
    assert any(why in p for p in problems), problems


def test_comments_and_quoted_words_do_not_trip_the_read_check():
    sql = "-- update this later\nSELECT channel FROM silver.signals WHERE channel = 'delete me'"
    assert decl.sql_problems(sql, allowed_schemas={"silver"}) == []


def test_each_declaration_problem_names_its_file(repo):
    (repo / "data" / "verbs" / "broken.toml").write_text(
        'name = "x"\nobject = "missing"\nsql = "SELECT 1"\n'
    )
    (repo / "data" / "silver" / "derived.toml").write_text(
        '[[derived]]\nname = "bad"\nexpression = "a ** b"\n'
    )
    problems = decl.load(kit.load(repo)).problems
    assert any(p.startswith("data/verbs/broken.toml") and "missing" in p for p in problems)
    assert any(p.startswith("data/silver/derived.toml") and "expression" in p for p in problems)


def test_check_without_a_medallion_reports_isolation_not_run_never_passed(repo, monkeypatch):
    monkeypatch.delenv("AXIOM_KIT_DSN", raising=False)
    report = check_kit(kit.load(repo))
    assert report["isolation"] == "not run"


def test_the_notebook_is_valid_python_and_calls_only_the_kits_api(repo):
    source = (repo / "data" / "notebooks" / "first-contribution.py").read_text()
    tree = ast.parse(source)
    called = {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "kit"
    }
    assert called and called <= set(kit.__all__), called - set(kit.__all__)


def test_the_example_chart_draws_one_unit():
    spec = json.loads((TEMPLATE / "charts" / "daily_overview.json").read_text())
    assert spec["only"] == ["temperature.inlet", "temperature.outlet"]


def test_the_five_verbs_reach_every_surface():
    from axiom.extensions.builtins.data_platform.skills import bind_default
    from axiom.infra.capability_projection import exposed_on, is_read_only

    registry = bind_default()
    for verb in ("kit_init", "kit_up", "kit_try", "kit_check", "kit_down"):
        spec = registry.spec(f"data.{verb}")
        assert all(exposed_on(spec, s) for s in ("cli", "mcp", "agent_tool")), verb
        assert not is_read_only(spec), f"{verb} writes; it must be confirm-gated"
