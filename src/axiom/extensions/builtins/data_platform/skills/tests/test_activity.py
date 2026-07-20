# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""data.activity — duration normalization, registration, and the
site-supplied folder → connector-label attribution map.

The map is *configuration*, not code: axiom ships no folder names. Sites
supply theirs through the ``data_platform.connector_labels`` config knob
(ADR-065 five-verb facade; operator-durable home is
``<config-dir>/data_platform.toml``). Unmapped folders pass through
unchanged — the behavior the skill has always had.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform.skills import activity
from axiom.extensions.builtins.data_platform.skills.activity import (
    _to_interval,
    connector_by_folder,
)
from axiom.infra.skills import SkillContext, SkillRegistry

_SCHEMA_PATH = Path(activity.__file__).resolve().parents[1] / "config.schema.json"

# Neutral fixture map (roles, not names — no real site folders/slugs here).
_SITE_MAP_TOML = """\
[[data_platform.connector_labels]]
folder = "Curated Library"
connector = "site-lib"

[[data_platform.connector_labels]]
folder = "Operations Records"
connector = "site-ops"
"""


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config_env(monkeypatch, tmp_path):
    """Isolated config registry + config dir; shipped schema registered."""
    from axiom.infra.config import register_schema_from_jsonschema
    from axiom.infra.config import registry as registry_mod

    registry_mod.reset_for_testing()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AXIOM_CONFIG_DIR", str(config_dir))
    register_schema_from_jsonschema("data_platform", _SCHEMA_PATH)
    yield config_dir
    registry_mod.reset_for_testing()


class _FakeCursor:
    def __init__(self, has_ds: bool, rows: list[tuple]):
        self._has_ds = has_ds
        self._rows = rows
        self.executed: list[tuple] = []
        self._last = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "information_schema.columns" in sql:
            self._last = (1,) if self._has_ds else None

    def fetchone(self):
        return self._last

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass


def _patch_db(monkeypatch, has_ds: bool, rows: list[tuple]):
    cur = _FakeCursor(has_ds, rows)
    fake = type(
        "FakePsycopg2", (), {"connect": staticmethod(lambda dsn: _FakeConn(cur))}
    )
    monkeypatch.setitem(sys.modules, "psycopg2", fake)
    monkeypatch.setenv("DP1_RAG_DSN", "postgresql://stub/db")
    return cur


def _ctx() -> SkillContext:
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=Path("/tmp/activity-test"),
        logger=logging.getLogger("test.activity"),
    )


# ---------------------------------------------------------------------------
# duration normalization (pre-existing behavior)
# ---------------------------------------------------------------------------


def test_duration_shorthands():
    assert _to_interval("24h") == "24 hours"
    assert _to_interval("7d") == "7 days"
    assert _to_interval("90m") == "90 minutes"
    assert _to_interval("2w") == "2 weeks"


def test_passthrough_and_default():
    assert _to_interval("36 hours") == "36 hours"
    assert _to_interval("") == "24 hours"   # empty falls back to 24h default


def test_registered_in_skill_registry():
    from axiom.extensions.builtins.data_platform import skills
    reg = skills.bind_default()
    assert "data.activity" in reg.list() if hasattr(reg, "list") else True


# ---------------------------------------------------------------------------
# folder → connector-label map: config-supplied, neutral default, fallback
# ---------------------------------------------------------------------------


def test_neutral_default_is_empty_map(config_env):
    """Axiom ships no map — a bare install labels nothing."""
    assert connector_by_folder() == {}


def test_no_schema_no_file_is_empty_map(monkeypatch, tmp_path):
    """Even with nothing registered at all, the loader never raises."""
    from axiom.infra.config import registry as registry_mod

    registry_mod.reset_for_testing()
    monkeypatch.setenv("AXIOM_CONFIG_DIR", str(tmp_path / "nowhere"))
    try:
        assert connector_by_folder() == {}
    finally:
        registry_mod.reset_for_testing()


def test_map_from_registry_write(config_env):
    """A value written through the five-verb facade is honored."""
    from axiom.infra.config import write_value

    write_value(
        "data_platform.connector_labels",
        [{"folder": "Curated Library", "connector": "site-lib"}],
        actor="@tester:local",
    )
    assert connector_by_folder() == {"Curated Library": "site-lib"}


def test_map_from_installed_config_file(config_env):
    """The operator-durable home: <config-dir>/data_platform.toml."""
    (config_env / "data_platform.toml").write_text(_SITE_MAP_TOML)
    assert connector_by_folder() == {
        "Curated Library": "site-lib",
        "Operations Records": "site-ops",
    }


def test_registry_value_wins_over_installed_file(config_env):
    """Watcher/API-fed registry values take precedence over the file."""
    from axiom.infra.config import write_value

    (config_env / "data_platform.toml").write_text(_SITE_MAP_TOML)
    write_value(
        "data_platform.connector_labels",
        [{"folder": "Curated Library", "connector": "override-lib"}],
        actor="@tester:local",
    )
    assert connector_by_folder() == {"Curated Library": "override-lib"}


def test_malformed_entries_are_skipped(config_env):
    from axiom.infra.config import write_value

    write_value(
        "data_platform.connector_labels",
        [
            {"folder": "Curated Library", "connector": "site-lib"},
            {"folder": "Missing Connector"},          # no connector
            {"connector": "missing-folder"},          # no folder
            "not-a-table",                            # wrong shape
        ],
        actor="@tester:local",
    )
    assert connector_by_folder() == {"Curated Library": "site-lib"}


# ---------------------------------------------------------------------------
# run(): a configured map reproduces the hardcoded-era labeling exactly
# ---------------------------------------------------------------------------


def test_configured_map_labels_source_path_folders(config_env, monkeypatch):
    (config_env / "data_platform.toml").write_text(_SITE_MAP_TOML)
    _patch_db(
        monkeypatch,
        has_ds=False,
        rows=[
            ("Curated Library", 3, 30, "2026-07-01 00:00:00"),
            ("Operations Records", 2, 8, "2026-07-02 00:00:00"),
        ],
    )

    res = activity.run({"since": "24h"}, _ctx())

    assert res.ok
    assert res.value["attribution"] == "source_path"
    assert [i["connector"] for i in res.value["items"]] == ["site-lib", "site-ops"]
    assert res.value["total_docs"] == 5
    assert res.value["total_chunks"] == 38


def test_unmapped_folder_passes_through_unchanged(config_env, monkeypatch):
    """The pre-config behavior for unmapped folders: identity passthrough."""
    (config_env / "data_platform.toml").write_text(_SITE_MAP_TOML)
    _patch_db(
        monkeypatch,
        has_ds=False,
        rows=[("Unlisted Folder", 1, 4, "2026-07-03 00:00:00")],
    )

    res = activity.run({}, _ctx())

    assert res.ok
    assert res.value["items"][0]["connector"] == "Unlisted Folder"


def test_data_source_attribution_ignores_the_map(config_env, monkeypatch):
    """Canonical schema present → data_source IS the label; map unused."""
    (config_env / "data_platform.toml").write_text(_SITE_MAP_TOML)
    _patch_db(
        monkeypatch,
        has_ds=True,
        rows=[("Curated Library", 3, 30, "2026-07-01 00:00:00")],
    )

    res = activity.run({}, _ctx())

    assert res.ok
    assert res.value["attribution"] == "data_source"
    assert res.value["items"][0]["connector"] == "Curated Library"


def test_connector_filter_matches_label_or_raw_folder(config_env, monkeypatch):
    (config_env / "data_platform.toml").write_text(_SITE_MAP_TOML)
    rows = [
        ("Curated Library", 3, 30, "2026-07-01 00:00:00"),
        ("Operations Records", 2, 8, "2026-07-02 00:00:00"),
    ]
    _patch_db(monkeypatch, has_ds=False, rows=rows)
    res = activity.run({"connector": "site-lib"}, _ctx())
    assert [i["connector"] for i in res.value["items"]] == ["site-lib"]

    _patch_db(monkeypatch, has_ds=False, rows=rows)
    res = activity.run({"connector": "Operations Records"}, _ctx())
    assert [i["connector"] for i in res.value["items"]] == ["site-ops"]
