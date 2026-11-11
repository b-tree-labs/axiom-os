# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data.table`` and ``data.profile`` as verbs: declared, bounded, and drawn alike everywhere (ADR-217).

The terminal prints ``text``, chat recognises the result as a table by its
``spec`` and ``text``, MCP lists the verb as a read with a cost class, and a
bound install forwards it to its node like every other gold read.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from axiom.extensions.builtins.data_platform import skills
from axiom.extensions.builtins.data_platform import table_door as td
from axiom.extensions.builtins.data_platform.read_target import FOLLOWS_THE_BINDING
from axiom.extensions.builtins.data_platform.skills import gold
from axiom.extensions.builtins.scidisplay import table_spec as ts
from axiom.infra.principal import PrincipalContext
from axiom.infra.skills import SkillContext, SkillRegistry

psycopg2 = pytest.importorskip("psycopg2")

TABLE = f"table_verbs_{uuid.uuid4().hex[:8]}"


def _ctx(tmp_path, surface="mcp"):
    return SkillContext(registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("t"),
                        principal=PrincipalContext(handle="@tester", posture="attested", assured=True),
                        surface=surface)


class TestDeclared:
    def test_both_verbs_are_reads_on_every_agent_surface_with_a_cost_class(self):
        reg = SkillRegistry()
        skills.bind(reg)
        for name, cost in (("data.table", "series"), ("data.profile", "aggregate")):
            spec = reg.get_spec(name) if hasattr(reg, "get_spec") else reg.spec(name)
            assert {"cli", "mcp", "agent_tool"} <= set(spec.surfaces)
            assert spec.cost_class == cost and spec.side_effects is False

    def test_a_bound_install_forwards_them(self):
        assert {"data.table", "data.profile"} <= set(FOLLOWS_THE_BINDING)


class TestNoDatabaseNeeded:
    def test_a_value_is_never_sql_text(self):
        rel = td.Relation("t", 1, {"note": td.ColumnInfo("note", "text", False),
                                   "v": td.ColumnInfo("v", "double precision", False)}, (), ("v",), 10, False)
        sql, params = td.compile_predicate(rel, ts.Predicate("note", "contains", "x'; DROP TABLE t; --"))
        assert "DROP" not in sql and params == ["%x''; DROP TABLE t; --%".replace("''", "'")]
        sql, params = td.compile_predicate(rel, ts.Predicate("v", "between", (1, 2)))
        assert sql == '"v" BETWEEN CAST(%s AS double precision) AND CAST(%s AS double precision)'
        assert params == [1, 2]

    def test_like_wildcards_in_a_value_are_literal(self):
        rel = td.Relation("t", 1, {"note": td.ColumnInfo("note", "text", False)}, (), ("note",), 10, False)
        _, params = td.compile_predicate(rel, ts.Predicate("note", "starts_with", "50%_off"))
        assert params == ["50\\%\\_off%"]

    def test_a_tampered_cursor_is_refused(self):
        with pytest.raises(td.TableDoorError, match="not one this door issued"):
            td.decode_cursor("not-a-cursor", "abc")
        good = td.encode_cursor("f" * 64, "all", [1, "a"])
        assert td.decode_cursor(good, "f" * 64) == ("all", [1, "a"])
        with pytest.raises(td.TableDoorError, match="different sort or filter"):
            td.decode_cursor(good, "0" * 64)

    def test_nulls_sort_last_in_the_keyset_predicate(self):
        rel = td.Relation("t", 1, {"a": td.ColumnInfo("a", "integer", False),
                                   "k": td.ColumnInfo("k", "integer", True)}, (), ("k",), 10, False)
        keys = (td.OrderKey("a", True, True), td.OrderKey("k", True, False, tiebreak=True))
        sql, params = td.keyset_predicate(rel, keys, [5, 9])
        assert '"a" IS NULL' in sql  # after a value, the nulls are still to come
        sql, _ = td.keyset_predicate(rel, keys, [None, 9])
        assert '"a" IS NULL AND "k" < ' in sql  # inside the nulls, only the key moves


@pytest.fixture(scope="module")
def dsn(_scratch_postgres_url):
    from axiom.infra.db import libpq_url

    url = libpq_url(_scratch_postgres_url)
    conn = psycopg2.connect(url)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("CREATE SCHEMA IF NOT EXISTS gold")
    c.execute(f"CREATE TABLE gold.{TABLE} (id int PRIMARY KEY, ts timestamptz NOT NULL, value float8, "
              f"sigma float8, quality text NOT NULL, quality_reason text)")
    c.execute(f"INSERT INTO gold.{TABLE} SELECT i, timestamptz '2026-10-01' + make_interval(secs => i), "
              f"i * 1.5, 0.25, CASE WHEN i = 7 THEN 'bad' ELSE 'good' END, "
              f"CASE WHEN i = 7 THEN 'sentinel:0' END FROM generate_series(1, 500) i")
    c.execute(f"CREATE INDEX ON gold.{TABLE} (ts)")
    c.execute(f"ANALYZE gold.{TABLE}")
    yield url
    c.execute(f"DROP TABLE gold.{TABLE}")
    conn.close()


class TestOnEverySurface:
    def _door(self, **over):
        doc = {"schema_version": "2.0", "kind": "rows", "title": "Readings",
               "source": {"type": "verb", "verb": "data.table", "args": {"dataset": TABLE}},
               "columns": [{"id": "ts", "label": "Time", "sortable": True, "type": "time"},
                           {"id": "value", "label": "Value", "type": "quantity", "unit": "kW",
                            "uncertainty_from": "sigma"},
                           {"id": "quality", "label": "Quality", "type": "quality"}],
               "sort": [{"column": "ts", "direction": "desc"}],
               "window": {"offset": 0, "limit": 10, "cursor": None}}
        doc.update(over)
        return doc

    def test_one_window_carries_its_text_and_the_chat_sees_a_table(self, dsn, monkeypatch, tmp_path):
        monkeypatch.setenv("DATABASE_URL", dsn)
        result = gold.table({"spec": self._door()}, _ctx(tmp_path))
        assert result.ok, result.errors
        value = result.value
        assert len(value["rows"]) == 10 and value["next"]
        assert value["count"] == {"kind": "exact", "n": 500, "method": "bounded probe (<= 100,000 rows)"}
        assert "rows 1–10 of 500 rows" in value["text"] and "750.00 ± 0.25 kW" in value["text"]
        from axiom.extensions.builtins.chat.providers.base import table_view_text

        assert table_view_text({"ok": True, "value": value}) == value["text"]

    def test_the_friendly_form_a_terminal_types(self, dsn, monkeypatch, tmp_path):
        monkeypatch.setenv("DATABASE_URL", dsn)
        result = gold.table({"dataset": TABLE, "sort": ["value:desc"], "where": ["quality eq bad"],
                             "limit": 5}, _ctx(tmp_path, "cli"))
        assert result.ok, result.errors
        assert [r["id"] for r in result.value["rows"]] == [7]
        assert "bad (sentinel:0)" in result.value["text"]

    def test_a_small_relation_sorts_on_anything(self, dsn, monkeypatch, tmp_path):
        monkeypatch.setenv("DATABASE_URL", dsn)
        result = gold.table({"dataset": TABLE, "sort": ["quality"]}, _ctx(tmp_path))
        assert result.ok and "is small" in result.value["applied"]["order"]

    def test_a_refusal_is_a_message_not_a_traceback(self, dsn, monkeypatch, tmp_path):
        monkeypatch.setenv("DATABASE_URL", dsn)
        monkeypatch.setattr(td, "CHEAP_RELATION_ROWS", 10)
        result = gold.table({"dataset": TABLE, "sort": ["quality"]}, _ctx(tmp_path))
        assert not result.ok and "An index on (quality) would serve it" in result.errors[0]

    def test_an_agent_gets_the_agent_budget(self, dsn, monkeypatch, tmp_path):
        monkeypatch.setenv("DATABASE_URL", dsn)
        doc = self._door(window={"offset": 0, "limit": 1000, "cursor": None})
        result = gold.table({"spec": doc}, _ctx(tmp_path, "mcp"))
        assert result.ok
        assert len(result.value["rows"]) == 500  # the whole (small) result: under the agent's 1,000 cap

    def test_the_profile_verb(self, dsn, monkeypatch, tmp_path):
        monkeypatch.setenv("DATABASE_URL", dsn)
        result = gold.profile({"spec": self._door(), "column": "value"}, _ctx(tmp_path))
        assert result.ok, result.errors
        assert result.value["min"] == 1.5 and result.value["max"] == 750
        assert result.value["basis"]["kind"] == "exact" and result.value["flags"] == {"bad": 1, "suspect": 0}
