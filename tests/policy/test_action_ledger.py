# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``axiom.policy.action_ledger`` (issue #665).

The action-provenance ledger is the searchable store behind
``guarded_act``'s decision provenance. Contract under test:

  - SQL backend (Postgres in production; exercised here on SQLite via an
    injected engine so no live PG is required) with indexes on
    (agent, ts) and (op_class, ts).
  - JSONL degraded fallback when SQL is unavailable.
  - Provenance is ON in standard mode (deliberate divergence from
    AuditLog's EC-only posture) — plain records without a chain key,
    HMAC-chained records when a chain key is present.
  - Query service: agent / op_class / outcome / since / until / text
    filters, most-recent-first, bounded limit.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    from axiom.policy import action_ledger
    action_ledger._reset_backend_cache()
    yield
    action_ledger._reset_backend_cache()


def _jsonl_ledger(tmp_path, monkeypatch, hmac_key=None):
    from axiom.policy.action_ledger import ActionLedger
    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    return ActionLedger(state_dir=tmp_path, hmac_key=hmac_key)


def _sql_ledger(tmp_path, hmac_key=None):
    from axiom.policy.action_ledger import ActionLedger
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}", future=True)
    return ActionLedger(state_dir=tmp_path, hmac_key=hmac_key, engine=engine)


def _record(ledger, **overrides):
    kwargs = dict(
        agent="tidy",
        op_class="git.branch.delete",
        name="prune_merged",
        candidate="feat/x",
        guards=["hard_disable", "pause", "volume", "act"],
        outcome="proceeded",
    )
    kwargs.update(overrides)
    return ledger.record_action(**kwargs)


# ---------------------------------------------------------------------------
# Write + read back — both backends
# ---------------------------------------------------------------------------


class TestWriteQuery:

    @pytest.mark.parametrize("kind", ["jsonl", "sql"])
    def test_roundtrip_all_fields(self, tmp_path, monkeypatch, kind):
        ledger = (
            _jsonl_ledger(tmp_path, monkeypatch) if kind == "jsonl"
            else _sql_ledger(tmp_path)
        )
        _record(
            ledger,
            candidate="feat/old",
            refusing_rule=None,
            dry_run=False,
            undo_ref="refs/tidy-archive/local/feat/old",
            metadata={"repo": "/tmp/r"},
        )
        rows = ledger.query()
        assert len(rows) == 1
        row = rows[0]
        assert row["agent"] == "tidy"
        assert row["op_class"] == "git.branch.delete"
        assert row["name"] == "prune_merged"
        assert row["candidate"] == "feat/old"
        assert row["outcome"] == "proceeded"
        assert row["refusing_rule"] is None
        assert row["dry_run"] is False
        assert row["undo_ref"] == "refs/tidy-archive/local/feat/old"
        assert row["metadata"] == {"repo": "/tmp/r"}
        assert row["guards"] == ["hard_disable", "pause", "volume", "act"]
        assert row["id"]
        assert row["ts"]  # ISO-8601 string

    @pytest.mark.parametrize("kind", ["jsonl", "sql"])
    def test_refusal_is_first_class(self, tmp_path, monkeypatch, kind):
        ledger = (
            _jsonl_ledger(tmp_path, monkeypatch) if kind == "jsonl"
            else _sql_ledger(tmp_path)
        )
        _record(ledger, outcome="refused", refusing_rule="hard_disable")
        rows = ledger.query(outcome="refused")
        assert len(rows) == 1
        assert rows[0]["refusing_rule"] == "hard_disable"

    @pytest.mark.parametrize("kind", ["jsonl", "sql"])
    def test_filters_and_compose(self, tmp_path, monkeypatch, kind):
        ledger = (
            _jsonl_ledger(tmp_path, monkeypatch) if kind == "jsonl"
            else _sql_ledger(tmp_path)
        )
        _record(ledger, agent="tidy", op_class="git.branch.delete", candidate="a")
        _record(ledger, agent="rivet", op_class="github.issue.close",
                candidate="b", outcome="failed")
        _record(ledger, agent="tidy", op_class="artifact.delete",
                candidate="c", outcome="refused", refusing_rule="paused:all")

        assert len(ledger.query(agent="tidy")) == 2
        assert len(ledger.query(op_class="github.issue.close")) == 1
        assert len(ledger.query(agent="tidy", outcome="refused")) == 1
        assert ledger.query(text="paused")[0]["candidate"] == "c"

    @pytest.mark.parametrize("kind", ["jsonl", "sql"])
    def test_since_until_and_limit(self, tmp_path, monkeypatch, kind):
        ledger = (
            _jsonl_ledger(tmp_path, monkeypatch) if kind == "jsonl"
            else _sql_ledger(tmp_path)
        )
        now = datetime.now(UTC)
        _record(ledger, candidate="old", ts=now - timedelta(days=10))
        _record(ledger, candidate="mid", ts=now - timedelta(days=2))
        _record(ledger, candidate="new", ts=now - timedelta(minutes=5))

        assert {r["candidate"] for r in ledger.query(since="7d")} == {"mid", "new"}
        assert {r["candidate"] for r in ledger.query(since="7d", until="1d")} == {"mid"}
        # ISO-8601 also accepted
        iso = (now - timedelta(days=7)).isoformat()
        assert len(ledger.query(since=iso)) == 2
        # Most recent first + bounded
        assert [r["candidate"] for r in ledger.query(limit=1)] == ["new"]

    @pytest.mark.parametrize("kind", ["jsonl", "sql"])
    def test_recent_scopes_by_agent(self, tmp_path, monkeypatch, kind):
        ledger = (
            _jsonl_ledger(tmp_path, monkeypatch) if kind == "jsonl"
            else _sql_ledger(tmp_path)
        )
        for i in range(3):
            _record(ledger, agent="tidy", candidate=f"t{i}")
        _record(ledger, agent="rivet", candidate="r0")
        rows = ledger.recent(n=2, agent="tidy")
        assert len(rows) == 2
        assert all(r["agent"] == "tidy" for r in rows)

    def test_bad_since_raises_value_error(self, tmp_path, monkeypatch):
        ledger = _jsonl_ledger(tmp_path, monkeypatch)
        with pytest.raises(ValueError):
            ledger.query(since="not-a-time")


# ---------------------------------------------------------------------------
# Backend selection: SQL default, JSONL degraded fallback
# ---------------------------------------------------------------------------


class TestBackendSelection:

    def test_jsonl_env_forces_flat_file(self, tmp_path, monkeypatch):
        ledger = _jsonl_ledger(tmp_path, monkeypatch)
        _record(ledger)
        assert ledger.backend_name == "jsonl"
        path = tmp_path / "audit" / "actions.jsonl"
        assert path.exists()
        assert json.loads(path.read_text().splitlines()[0])["agent"] == "tidy"

    def test_injected_engine_uses_sql(self, tmp_path):
        ledger = _sql_ledger(tmp_path)
        assert ledger.backend_name == "sql"
        _record(ledger)
        assert len(ledger.query()) == 1

    def test_sql_backend_has_required_indexes(self, tmp_path):
        ledger = _sql_ledger(tmp_path)
        _record(ledger)  # ensures DDL ran
        engine = ledger._backend._engine
        idx = {
            tuple(i["column_names"]): i["name"]
            for i in inspect(engine).get_indexes("agent_actions")
        }
        assert ("agent", "ts") in idx
        assert ("op_class", "ts") in idx

    def test_auto_falls_back_to_jsonl_when_sql_unavailable(
        self, tmp_path, monkeypatch,
    ):
        from axiom.policy.action_ledger import ActionLedger

        def _boom():
            raise RuntimeError("no database")

        monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "auto")
        monkeypatch.setattr("axiom.infra.db.get_engine", _boom)
        ledger = ActionLedger(state_dir=tmp_path)
        assert ledger.backend_name == "jsonl"
        _record(ledger)
        assert len(ledger.query()) == 1

    def test_auto_prefers_sql_when_engine_available(self, tmp_path, monkeypatch):
        from axiom.policy.action_ledger import ActionLedger

        engine = create_engine(f"sqlite:///{tmp_path / 'auto.db'}", future=True)
        monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "auto")
        monkeypatch.setattr("axiom.infra.db.get_engine", lambda: engine)
        ledger = ActionLedger(state_dir=tmp_path)
        assert ledger.backend_name == "sql"


# ---------------------------------------------------------------------------
# HMAC chain (EC mode) — plain rows in standard mode, chained with a key
# ---------------------------------------------------------------------------


class TestChain:

    @pytest.mark.parametrize("kind", ["jsonl", "sql"])
    def test_no_key_writes_plain_records(self, tmp_path, monkeypatch, kind):
        ledger = (
            _jsonl_ledger(tmp_path, monkeypatch) if kind == "jsonl"
            else _sql_ledger(tmp_path)
        )
        _record(ledger)
        assert ledger.query()[0].get("hmac") in (None, "")
        ok, broken = ledger.verify_chain()
        assert ok is True and broken is None

    @pytest.mark.parametrize("kind", ["jsonl", "sql"])
    def test_key_chains_and_verifies(self, tmp_path, monkeypatch, kind):
        ledger = (
            _jsonl_ledger(tmp_path, monkeypatch, hmac_key="k1") if kind == "jsonl"
            else _sql_ledger(tmp_path, hmac_key="k1")
        )
        for i in range(3):
            _record(ledger, candidate=f"c{i}")
        ok, broken = ledger.verify_chain()
        assert ok is True and broken is None

    def test_tamper_detected_jsonl(self, tmp_path, monkeypatch):
        ledger = _jsonl_ledger(tmp_path, monkeypatch, hmac_key="k1")
        for i in range(3):
            _record(ledger, candidate=f"c{i}")
        path = tmp_path / "audit" / "actions.jsonl"
        lines = path.read_text().splitlines()
        rec = json.loads(lines[1])
        rec["candidate"] = "TAMPERED"
        lines[1] = json.dumps(rec)
        path.write_text("\n".join(lines) + "\n")
        ok, broken = ledger.verify_chain()
        assert ok is False and broken == 1

    def test_tamper_detected_sql(self, tmp_path):
        ledger = _sql_ledger(tmp_path, hmac_key="k1")
        for i in range(3):
            _record(ledger, candidate=f"c{i}")
        engine = ledger._backend._engine
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE agent_actions SET candidate = 'TAMPERED' "
                "WHERE candidate = 'c1'"
            ))
        ok, broken = ledger.verify_chain()
        assert ok is False and broken == 1

    def test_chain_resumes_across_instances(self, tmp_path, monkeypatch):
        ledger = _jsonl_ledger(tmp_path, monkeypatch, hmac_key="k1")
        _record(ledger, candidate="a")
        # New instance must pick the chain up from the stored tail.
        ledger2 = _jsonl_ledger(tmp_path, monkeypatch, hmac_key="k1")
        _record(ledger2, candidate="b")
        ok, broken = ledger2.verify_chain()
        assert ok is True and broken is None


# ---------------------------------------------------------------------------
# Module-level query service (MCP / CLI / chat delegate here)
# ---------------------------------------------------------------------------


class TestQueryService:

    def test_search_actions_payload_shape(self, tmp_path, monkeypatch):
        from axiom.policy.action_ledger import search_actions
        ledger = _jsonl_ledger(tmp_path, monkeypatch)
        _record(ledger, candidate="x", outcome="refused",
                refusing_rule="volume_limit_exceeded (12 > 10)")
        out = search_actions(outcome="refused", state_dir=tmp_path)
        assert out["count"] == 1
        assert out["backend"] == "jsonl"
        assert out["actions"][0]["candidate"] == "x"

    def test_recent_actions_payload_shape(self, tmp_path, monkeypatch):
        from axiom.policy.action_ledger import recent_actions
        ledger = _jsonl_ledger(tmp_path, monkeypatch)
        for i in range(4):
            _record(ledger, candidate=f"c{i}")
        out = recent_actions(n=2, state_dir=tmp_path)
        assert out["count"] == 2
        assert out["actions"][0]["candidate"] == "c3"

    def test_search_actions_bad_since_is_clean_error(self, tmp_path, monkeypatch):
        from axiom.policy.action_ledger import search_actions
        _jsonl_ledger(tmp_path, monkeypatch)
        out = search_actions(since="garbage", state_dir=tmp_path)
        assert out.get("error")

    def test_verify_actions_payload(self, tmp_path, monkeypatch):
        from axiom.policy.action_ledger import verify_actions
        monkeypatch.setenv("AXIOM_AUDIT_HMAC_KEY", "k1")
        ledger = _jsonl_ledger(tmp_path, monkeypatch, hmac_key="k1")
        _record(ledger)
        out = verify_actions(state_dir=tmp_path)
        assert out["ok"] is True
        assert out["broken_at"] is None
