# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``data.refresh`` — CDC / incremental delta ingest.

Pins the load-bearing logic: the per-connector watermark derivation
(``max(indexed_at)`` minus ``overlap_minutes``, with a full-pass fallback when
there's no prior watermark) and skill registration. The real ``run_ingest`` is
stubbed — these tests never touch a source, Postgres, or the embedder.

The source_path fallback scoping uses the site-supplied
``data_platform.connector_labels`` config map (see ``skills/activity.py``);
these tests configure a neutral fixture map rather than any real site's.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform.agents.plinth.skills.run_ingest import (
    RunIngestReport,
)
from axiom.extensions.builtins.data_platform.skills import activity, refresh
from axiom.infra.skills import SkillContext, SkillRegistry

_SCHEMA_PATH = Path(activity.__file__).resolve().parents[1] / "config.schema.json"


class _FakeCursor:
    def __init__(self, has_ds: bool, watermark):
        self._has_ds = has_ds
        self._watermark = watermark
        self.executed: list[tuple] = []
        self._last = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "information_schema.columns" in sql:
            self._last = (1,) if self._has_ds else None
        elif "max(indexed_at)" in sql:
            self._last = (self._watermark,)
        else:  # pragma: no cover - defensive
            self._last = None

    def fetchone(self):
        return self._last

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


def _ctx() -> SkillContext:
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=Path("/tmp/refresh-test"),
        logger=logging.getLogger("test.refresh"),
    )


@pytest.fixture
def _captured(monkeypatch):
    """Stub psycopg2 + run_ingest; capture the ``since`` handed to ingest."""
    captured: dict = {}

    def _stub_run_ingest(connector, *, since, state_dir, volume_mode, max_workers):
        captured["connector"] = connector
        captured["since"] = since
        captured["volume_mode"] = volume_mode
        captured["max_workers"] = max_workers
        return RunIngestReport(
            connector=connector, proceed=True,
            items_seen=3, items_landed=2, items_failed=0,
        )

    monkeypatch.setattr(refresh, "run_ingest", _stub_run_ingest)
    monkeypatch.setenv("DP1_RAG_DSN", "postgresql://stub/db")
    return captured


def _patch_db(monkeypatch, has_ds, watermark):
    cur = _FakeCursor(has_ds, watermark)
    fake = type("FakePsycopg2", (), {
        "connect": staticmethod(lambda dsn: _FakeConn(cur)),
    })
    monkeypatch.setitem(__import__("sys").modules, "psycopg2", fake)
    return cur


@pytest.fixture
def _site_map(monkeypatch, tmp_path):
    """A neutral site-supplied folder → connector map (roles, not names)."""
    from axiom.infra.config import register_schema_from_jsonschema
    from axiom.infra.config import registry as registry_mod

    registry_mod.reset_for_testing()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AXIOM_CONFIG_DIR", str(config_dir))
    register_schema_from_jsonschema("data_platform", _SCHEMA_PATH)
    (config_dir / "data_platform.toml").write_text(
        '[[data_platform.connector_labels]]\n'
        'folder = "Curated Library"\n'
        'connector = "corpus-a"\n'
    )
    yield
    registry_mod.reset_for_testing()


def test_watermark_minus_overlap_is_the_since(_captured, monkeypatch):
    wm = datetime(2026, 6, 25, 12, 0, 0)
    _patch_db(monkeypatch, has_ds=True, watermark=wm)

    res = refresh.run({"connector": "corpus-a", "overlap_minutes": 60}, _ctx())

    assert res.ok
    assert _captured["since"] == wm - timedelta(minutes=60)
    assert _captured["volume_mode"] == "off"
    assert _captured["max_workers"] == 8
    assert res.value["full_pass"] is False
    assert res.value["watermark"] == wm.isoformat()


def test_no_prior_watermark_is_full_pass(_captured, monkeypatch):
    _patch_db(monkeypatch, has_ds=True, watermark=None)

    res = refresh.run({"connector": "corpus-a"}, _ctx())

    assert res.ok
    assert _captured["since"] is None
    assert res.value["full_pass"] is True
    assert res.value["watermark"] is None


def test_default_overlap_is_60_minutes(_captured, monkeypatch):
    wm = datetime(2026, 6, 25, 12, 0, 0)
    _patch_db(monkeypatch, has_ds=True, watermark=wm)

    refresh.run({"connector": "corpus-a"}, _ctx())

    assert _captured["since"] == wm - timedelta(minutes=60)


def test_source_path_fallback_uses_folder_for_mapped_connector(
    _captured, _site_map, monkeypatch
):
    wm = datetime(2026, 6, 25, 12, 0, 0)
    cur = _patch_db(monkeypatch, has_ds=False, watermark=wm)

    refresh.run({"connector": "corpus-a"}, _ctx())

    # The watermark query must scope by source_path LIKE the mapped folder.
    path_query = [e for e in cur.executed if "source_path LIKE" in e[0]]
    assert path_query, "expected a source_path-scoped watermark query"
    assert path_query[0][1] == ("Curated Library",)


def test_source_path_fallback_unmapped_connector_uses_its_own_name(
    _captured, monkeypatch, tmp_path
):
    """No site map → the connector name itself scopes the query
    (the behavior the skill has always had for unmapped connectors)."""
    from axiom.infra.config import registry as registry_mod

    registry_mod.reset_for_testing()
    monkeypatch.setenv("AXIOM_CONFIG_DIR", str(tmp_path / "empty-config"))
    wm = datetime(2026, 6, 25, 12, 0, 0)
    cur = _patch_db(monkeypatch, has_ds=False, watermark=wm)

    try:
        refresh.run({"connector": "corpus-a"}, _ctx())
    finally:
        registry_mod.reset_for_testing()

    path_query = [e for e in cur.executed if "source_path LIKE" in e[0]]
    assert path_query, "expected a source_path-scoped watermark query"
    assert path_query[0][1] == ("corpus-a",)


def test_missing_connector_errors():
    res = refresh.run({}, _ctx())
    assert not res.ok
    assert "connector" in res.errors[0]


def test_negative_overlap_rejected(_captured, monkeypatch):
    _patch_db(monkeypatch, has_ds=True, watermark=None)
    res = refresh.run({"connector": "corpus-a", "overlap_minutes": -5}, _ctx())
    assert not res.ok


def test_refresh_is_registered():
    from axiom.extensions.builtins.data_platform import skills as data_skills

    reg = SkillRegistry()
    data_skills.bind(reg)
    assert reg.has("data.refresh")
    assert "refresh" in data_skills.verbs()
