"""The whole loop on a real Postgres, and the isolation proof's negative control.

Needs Docker (or AXIOM_KIT_DSN). Marked ``integration``, so it runs where a
database is available rather than on every unit run.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform import kit
from axiom.extensions.builtins.data_platform.kit.medallion import connect, dsn
from axiom.extensions.builtins.data_platform.kit.project import KitError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (shutil.which("docker") or os.environ.get("AXIOM_KIT_DSN")),
        reason="needs Docker or AXIOM_KIT_DSN",
    ),
]


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    repo = tmp_path_factory.mktemp("site")
    kit.init(repo, "kit-itest")
    p = kit.load(repo)
    kit.up(p)
    yield p
    kit.down(p)


def test_every_tier_runs_on_the_examples(project):
    r = kit.try_kit(project)
    assert r["silver"]["rows_in"] == 1008 and r["silver"]["errored"] == 0
    assert r["silver"]["rows_out"] == 3 * 1008
    assert any(d.startswith("derived temperature.rise") for d in r["silver"]["declarations"])
    rises = [row["rise"] for row in r["verbs"]["temperature_rise_by_day"]]
    assert len(rises) == 7 and rises == sorted(rises), "the sample warms day on day"
    assert Path(r["charts"][0]["svg"]).read_text().startswith("<svg")


def test_the_examples_pass_the_gate_with_isolation_proven(project):
    report = kit.check_kit(project)
    assert report["ok"] is True
    assert report["isolation"] == []


def test_a_contribution_that_reaches_past_the_tenant_boundary_is_caught(project):
    """Negative control: without it, a proof that always passes looks like one that works."""
    d = dsn(project)
    with connect(d) as conn:
        conn.execute(
            "CREATE OR REPLACE FUNCTION gold.kit_itest_leak() RETURNS TABLE(channel text, total float8) "
            "LANGUAGE sql SECURITY DEFINER AS 'SELECT channel, sum(value) FROM silver.signals GROUP BY channel'"
        )
        conn.execute(f'GRANT EXECUTE ON FUNCTION gold.kit_itest_leak() TO "{project.role}"')
    gold = project.folder("gold")
    (gold / "leaky.sql").write_text("SELECT channel, total FROM gold.kit_itest_leak()")
    (gold / "leaky.toml").write_text('description = "negative control"')
    try:
        report = kit.check_kit(project)
        assert report["ok"] is False
        assert any(f.startswith("gold leaky:") for f in report["isolation"])
    finally:
        (gold / "leaky.sql").unlink()
        (gold / "leaky.toml").unlink()
        with connect(d) as conn:
            conn.execute("DROP FUNCTION IF EXISTS gold.kit_itest_leak() CASCADE")


def test_a_chart_that_mixes_units_is_refused_naming_its_file(project):
    chart = project.folder("charts") / "mixed.json"
    chart.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "kind": "timeseries",
                "site": project.tenant,
                "title": "mixed",
                "channels": ["temperature.inlet", "flow.primary"],
                "window": {"from": "2026-09-01T00:00:00+00:00", "to": "2026-09-08T00:00:00+00:00"},
                "transform": {"bucket": "1h", "agg": "mean"},
            }
        )
    )
    try:
        with pytest.raises(KitError, match=r"mixed\.json: draws .* on one axis"):
            kit.try_kit(project)
    finally:
        chart.unlink()


def test_a_tenant_object_over_a_shared_gold_view_works_and_stays_isolated(project):
    """Shared gold views run with the reader's rights, so building on one is safe."""
    gold = project.folder("gold")
    (gold / "latest.sql").write_text("SELECT channel, value, unit FROM gold.signals_latest")
    (gold / "latest.toml").write_text('description = "latest reading per channel"')
    try:
        report = kit.check_kit(project)
        assert report["ok"] is True, report
        assert report["isolation"] == []
    finally:
        (gold / "latest.sql").unlink()
        (gold / "latest.toml").unlink()


def _plant(project, channel, unit, values):
    """Rows for one extra channel under the test tenant, straight into silver."""
    from axiom.extensions.builtins.data_platform.kit.medallion import connect, dsn

    with connect(dsn(project)) as conn:
        for i, v in enumerate(values):
            conn.execute(
                "INSERT INTO silver.signals (site, feed, channel, ts, value, unit, schema_ref, row_hash)"
                " VALUES (%s, 'rig', %s, %s, %s, %s, 'test', %s) ON CONFLICT DO NOTHING",
                (
                    project.tenant,
                    channel,
                    f"2026-09-01T{i:02d}:00:00+00:00",
                    v,
                    unit,
                    f"plant-{channel}-{i}",
                ),
            )


def _chart(project, name, channels, **extra):
    doc = {
        "schema_version": "1.1",
        "kind": "timeseries",
        "site": project.tenant,
        "title": name,
        "channels": channels,
        "window": {"from": "2026-09-01T00:00:00+00:00", "to": "2026-09-02T00:00:00+00:00"},
        "transform": {"bucket": "1h", "agg": "mean"},
        **extra,
    }
    path = project.folder("charts") / f"{name}.json"
    path.write_text(json.dumps(doc))
    return path


def test_a_chart_draws_what_its_spec_pins(project):
    """display_units and series_colours reach the figure, not only the check."""
    from axiom.extensions.builtins.data_platform.kit import declarations as decl
    from axiom.extensions.builtins.data_platform.kit.medallion import dsn
    from axiom.extensions.builtins.data_platform.kit.runner import render_chart

    kit.try_kit(project)
    _plant(project, "heater.power", "W", [1500.0, 1600.0, 1700.0])
    path = _chart(
        project,
        "pinned",
        ["heater.power"],
        display_units={"W": "kW"},
        series_colours={"heater.power": "#123456"},
    )
    try:
        (chart,) = [c for c in decl.load(project).charts if c.name == "pinned"]
        svg = Path(render_chart(project, dsn(project), chart)["svg"]).read_text()
        assert "kW" in svg, "the declared display unit is on the axis"
        assert "#123456" in svg, "the declared colour is the line's colour"
    finally:
        path.unlink()


def test_declared_and_undeclared_units_never_share_an_axis(project):
    from axiom.extensions.builtins.data_platform.kit import declarations as decl
    from axiom.extensions.builtins.data_platform.kit.medallion import dsn
    from axiom.extensions.builtins.data_platform.kit.runner import render_chart

    kit.try_kit(project)
    _plant(project, "mystery.signal", None, [1.0, 2.0, 3.0])
    mixed = _chart(project, "mixed_declared", ["temperature.inlet", "mystery.signal"])
    alone = _chart(project, "undeclared_alone", ["mystery.signal"])
    try:
        charts = {c.name: c for c in decl.load(project).charts}
        with pytest.raises(KitError, match=r"mixed_declared\.json: draws .*unit not declared"):
            render_chart(project, dsn(project), charts["mixed_declared"])
        svg = Path(
            render_chart(project, dsn(project), charts["undeclared_alone"])["svg"]
        ).read_text()
        assert "unit not declared" in svg or "no unit" in svg, "labelled, never blank"
    finally:
        mixed.unlink()
        alone.unlink()


def test_a_scaffolded_chart_draws_on_the_next_kit_try(project):
    (rel,) = kit.scaffold_contribution(project, "chart", "scaffolded")
    try:
        report = kit.try_kit(project)
        (drawn,) = [c for c in report["charts"] if c["chart"] == "scaffolded"]
        assert drawn["rows"] > 0
    finally:
        (project.root / rel).unlink()
