"""The whole loop on a real Postgres, and the isolation proof's negative control.

Needs Docker (or AXIOM_KIT_DSN). Marked ``integration``, so it runs where a
database is available rather than on every unit run.
"""

from __future__ import annotations

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
    chart.write_text('{"object": "daily_stats", "x": "day", "y": "mean", "series": "channel"}')
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
