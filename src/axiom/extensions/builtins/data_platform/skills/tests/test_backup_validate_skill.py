# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``data.backup_validate`` — fail-closed backup validation.

All probes (pg_restore, scratch restore, row sanity) are monkeypatched;
no PostgreSQL client tools or live database are touched.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform import _herald
from axiom.extensions.builtins.data_platform.database.backup_policy import (
    BackupPolicy,
    save_backup_policy,
)
from axiom.extensions.builtins.data_platform.skills import (
    backup_validate as bv,
)
from axiom.infra.backup import create_backup
from axiom.infra.skills import SkillContext, SkillRegistry

_DSN = "postgresql://axiom:pw@localhost:5432/axiom_db"


def _ctx(tmp_path: Path) -> SkillContext:
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=tmp_path,
        logger=logging.getLogger("test.backup_validate"),
    )


def _events(monkeypatch) -> list[tuple[str, str]]:
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        _herald,
        "publish_event",
        lambda intent, summary, **kw: captured.append((intent, summary)) or "rcpt-test",
    )
    return captured


def _scratch_ops(monkeypatch) -> list[str]:
    """Stand in for the database calls around a restore; records the order they ran."""
    ops: list[str] = []
    monkeypatch.setattr(bv, "_recreate_scratch", lambda dsn, live: ops.append("recreate"))
    monkeypatch.setattr(bv, "_finish_restore", lambda dsn: ops.append("finish"))
    monkeypatch.setattr(bv, "_drop_scratch_database", lambda dsn: ops.append("drop"))
    return ops


def _seed_backup(tmp_path: Path, *, age_hours: float = 1.0, fmt: str = "custom"):
    target = tmp_path / "backups"
    save_backup_policy(BackupPolicy(target_root=str(target)), state_dir=tmp_path)
    result = create_backup(_DSN, output_dir=target, fmt=fmt, _dry_run=True)
    stamp = time.time() - age_hours * 3600
    os.utime(result.backup_path, (stamp, stamp))
    return target, result


def _toc_ok(monkeypatch):
    monkeypatch.setattr(bv, "_pg_restore_list", lambda p: (True, "TOC parses (5 entries)"))


def _check(result, name: str) -> dict:
    return next(c for c in result.value["checks"] if c["name"] == name)


class TestHappyPath:
    def test_fresh_valid_backup_passes(self, tmp_path, monkeypatch):
        events = _events(monkeypatch)
        _toc_ok(monkeypatch)
        _seed_backup(tmp_path, age_hours=1.0)
        result = bv.run({}, _ctx(tmp_path))
        assert result.ok
        assert events == []
        statuses = {c["name"]: c["status"] for c in result.value["checks"]}
        assert statuses == {
            "backup_exists": "PASS",
            "backup_fresh": "PASS",
            "backup_nonempty": "PASS",
            "toc_parses": "PASS",
            # Present, and WARN. This used to be four PASS rows with no mention
            # of restoring — a report that reads as complete and is not. All
            # four of those passed on an artifact that could not be restored:
            #   pg_restore: relation "public.documents" does not exist
            # `restore_live` is now always reported, and says which of the three
            # states it is in: proven, broken, or (here) unproven because no
            # scratch_dsn is configured to restore into.
            "restore_live": "WARN",
        }
        unproven = [c for c in result.value["checks"] if c["name"] == "restore_live"][0]
        assert "unproven" in unproven["detail"]
        assert "scratch_dsn" in (unproven.get("remediation") or "")
        # WARN, not FAIL: unverified is a different condition from broken, and
        # giving them one colour is how a red report becomes background noise.
        assert result.ok
        assert any(a.startswith("audit-receipt:") for a in result.actions_taken)

    def test_plain_format_warns_but_passes(self, tmp_path, monkeypatch):
        events = _events(monkeypatch)
        _seed_backup(tmp_path, age_hours=1.0, fmt="plain")
        result = bv.run({}, _ctx(tmp_path))
        assert result.ok  # WARN is not FAIL
        assert _check(result, "toc_parses")["status"] == "WARN"
        assert events == []


class TestStale:
    def test_stale_backup_fails_and_alerts(self, tmp_path, monkeypatch):
        events = _events(monkeypatch)
        _toc_ok(monkeypatch)
        _seed_backup(tmp_path, age_hours=48.0)
        result = bv.run({}, _ctx(tmp_path))
        assert not result.ok
        assert _check(result, "backup_fresh")["status"] == "FAIL"
        assert [e[0] for e in events] == ["data.backup.stale"]
        assert any("unusable" in e for e in result.errors)

    def test_threshold_param_overrides_default(self, tmp_path, monkeypatch):
        _events(monkeypatch)
        _toc_ok(monkeypatch)
        _seed_backup(tmp_path, age_hours=30.0)
        result = bv.run({"max_age_hours": 72}, _ctx(tmp_path))
        assert result.ok

    def test_bad_threshold_rejected(self, tmp_path, monkeypatch):
        _events(monkeypatch)
        result = bv.run({"max_age_hours": "tomorrow"}, _ctx(tmp_path))
        assert not result.ok
        assert any("max_age_hours" in e for e in result.errors)


class TestMissingOrBroken:
    def test_no_backups_fails_and_alerts(self, tmp_path, monkeypatch):
        events = _events(monkeypatch)
        save_backup_policy(BackupPolicy(target_root=str(tmp_path / "empty")), state_dir=tmp_path)
        result = bv.run({}, _ctx(tmp_path))
        assert not result.ok
        assert _check(result, "backup_exists")["status"] == "FAIL"
        assert [e[0] for e in events] == ["data.backup.validate_failed"]

    def test_zero_byte_artifact_fails(self, tmp_path, monkeypatch):
        events = _events(monkeypatch)
        _toc_ok(monkeypatch)
        _target, seeded = _seed_backup(tmp_path, age_hours=1.0)
        seeded.backup_path.write_bytes(b"")
        result = bv.run({}, _ctx(tmp_path))
        assert not result.ok
        assert _check(result, "backup_nonempty")["status"] == "FAIL"
        assert [e[0] for e in events] == ["data.backup.validate_failed"]

    def test_unparseable_toc_fails(self, tmp_path, monkeypatch):
        events = _events(monkeypatch)
        monkeypatch.setattr(
            bv, "_pg_restore_list", lambda p: (False, "not a custom-format archive")
        )
        _seed_backup(tmp_path, age_hours=1.0)
        result = bv.run({}, _ctx(tmp_path))
        assert not result.ok
        assert _check(result, "toc_parses")["status"] == "FAIL"
        assert [e[0] for e in events] == ["data.backup.validate_failed"]


@pytest.mark.acceptance("MVP-19-A4")
class TestLiveRestore:
    def test_requires_scratch_dsn(self, tmp_path, monkeypatch):
        _events(monkeypatch)
        _toc_ok(monkeypatch)
        _seed_backup(tmp_path, age_hours=1.0)
        result = bv.run({"validate_restore": True}, _ctx(tmp_path))
        assert not result.ok
        assert _check(result, "restore_live")["status"] == "FAIL"
        assert "scratch_dsn" in _check(result, "restore_live")["detail"]

    def test_scratch_restore_success(self, tmp_path, monkeypatch):
        _events(monkeypatch)
        _toc_ok(monkeypatch)
        _seed_backup(tmp_path, age_hours=1.0)
        ops = _scratch_ops(monkeypatch)
        monkeypatch.setattr(bv, "_restore_into_scratch", lambda p, dsn, fks: (True, "restored"))
        monkeypatch.setattr(bv, "_row_sanity", lambda scratch, live: (True, "12 user tables"))
        result = bv.run(
            {"validate_restore": True, "scratch_dsn": "postgresql://s/scratch"},
            _ctx(tmp_path),
        )
        assert result.ok
        assert _check(result, "restore_live")["status"] == "PASS"
        assert ops == ["recreate", "finish", "drop"]

    def test_scratch_restore_failure_fails_closed(self, tmp_path, monkeypatch):
        events = _events(monkeypatch)
        _toc_ok(monkeypatch)
        _seed_backup(tmp_path, age_hours=1.0)
        ops = _scratch_ops(monkeypatch)
        monkeypatch.setattr(
            bv, "_restore_into_scratch", lambda p, dsn, fks: (False, "restore exploded")
        )
        result = bv.run(
            {"validate_restore": True, "scratch_dsn": "postgresql://s/scratch"},
            _ctx(tmp_path),
        )
        assert not result.ok
        assert _check(result, "restore_live")["status"] == "FAIL"
        assert [e[0] for e in events] == ["data.backup.validate_failed"]
        assert ops == ["recreate", "drop"]  # a failed restore is still removed

    def test_row_sanity_failure_fails_closed(self, tmp_path, monkeypatch):
        _events(monkeypatch)
        _toc_ok(monkeypatch)
        _seed_backup(tmp_path, age_hours=1.0)
        monkeypatch.setattr(bv, "_restore_into_scratch", lambda p, dsn, fks: (True, "restored"))
        monkeypatch.setattr(
            bv,
            "_row_sanity",
            lambda scratch, live: (False, "scratch restore produced zero user tables"),
        )
        _scratch_ops(monkeypatch)
        result = bv.run(
            {"validate_restore": True, "scratch_dsn": "postgresql://s/scratch"},
            _ctx(tmp_path),
        )
        assert not result.ok

    def test_cleanup_failure_fails_the_check(self, tmp_path, monkeypatch):
        """Swallowing this stacked eight restores into a scratch database, 232 GB of it."""
        events = _events(monkeypatch)
        _toc_ok(monkeypatch)
        _seed_backup(tmp_path, age_hours=1.0)
        _scratch_ops(monkeypatch)
        monkeypatch.setattr(bv, "_restore_into_scratch", lambda p, dsn, fks: (True, "restored"))
        monkeypatch.setattr(bv, "_row_sanity", lambda scratch, live: (True, "12 user tables"))

        def refuse(dsn):
            raise RuntimeError("database is in use")

        monkeypatch.setattr(bv, "_drop_scratch_database", refuse)
        result = bv.run(
            {"validate_restore": True, "scratch_dsn": "postgresql://s/scratch"},
            _ctx(tmp_path),
        )
        assert not result.ok
        check = _check(result, "restore_live")
        assert check["status"] == "FAIL"
        assert "NOT dropped" in check["detail"]
        assert [e[0] for e in events] == ["data.backup.validate_failed"]

    def test_unpreparable_scratch_fails_without_restoring(self, tmp_path, monkeypatch):
        _events(monkeypatch)
        _toc_ok(monkeypatch)
        _seed_backup(tmp_path, age_hours=1.0)
        _scratch_ops(monkeypatch)
        restored: list[str] = []

        def refuse(dsn, live):
            raise PermissionError("permission denied to create database")

        monkeypatch.setattr(bv, "_recreate_scratch", refuse)
        monkeypatch.setattr(
            bv, "_restore_into_scratch", lambda p, dsn, fks: restored.append(dsn) or (True, "x")
        )
        result = bv.run(
            {"validate_restore": True, "scratch_dsn": "postgresql://s/scratch"},
            _ctx(tmp_path),
        )
        assert not result.ok
        assert restored == []

    def test_foreign_key_onto_excluded_data_is_not_a_failure(self):
        ok, detail = bv._judge_restore_errors(
            'pg_restore: error: could not execute query: ERROR:  insert or update on table "u" '
            'violates foreign key constraint "u_fkey"\n'
            "pg_restore: warning: errors ignored on restore: 1\n",
            frozenset({"u_fkey"}),
        )
        assert ok is True
        assert "u_fkey" in detail

    @pytest.mark.parametrize(
        "stderr",
        [
            'pg_restore: error: could not execute query: ERROR:  violates foreign key constraint "other"',
            'pg_restore: error: could not execute query: ERROR:  schema "attest" already exists',
            "pg_restore: warning: nothing named an error line\n",
        ],
    )
    def test_any_other_restore_error_fails_closed(self, stderr):
        ok, detail = bv._judge_restore_errors(stderr, frozenset({"u_fkey"}))
        assert ok is False
        assert detail.startswith("scratch restore failed")

    def test_one_unexpected_error_outweighs_expected_ones(self):
        stderr = (
            'pg_restore: error: x violates foreign key constraint "u_fkey"\n'
            'pg_restore: error: relation "gold.v" does not exist\n'
        )
        ok, detail = bv._judge_restore_errors(stderr, frozenset({"u_fkey"}))
        assert ok is False
        assert "gold.v" in detail

    def test_never_recreates_the_live_database(self):
        with pytest.raises(ValueError, match="live database"):
            bv._recreate_scratch("postgresql://u:p@h/axiom_db", "postgresql://u:p@h/axiom_db")

    @pytest.mark.parametrize("name", ["", "postgres"])
    def test_never_recreates_the_maintenance_database(self, name):
        with pytest.raises(ValueError, match="not a disposable"):
            bv._recreate_scratch(f"postgresql://u:p@h/{name}", None)

    def test_a_chunk_newer_than_the_dump_is_not_a_missing_table(self, monkeypatch):
        psycopg2 = pytest.importorskip("psycopg2")

        class Cur:
            def __init__(self, rows):
                self.rows = rows

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, q):
                pass

            def fetchall(self):
                return self.rows

        class Conn:
            def __init__(self, rows):
                self.rows = rows

            def cursor(self):
                return Cur(self.rows)

            def close(self):
                pass

        tables = {
            "scratch": [("silver.signals", 1)],
            "live": [("silver.signals", 1), ("_timescaledb_internal._hyper_2_9_chunk", 5)],
        }
        monkeypatch.setattr(psycopg2, "connect", lambda dsn, **kw: Conn(tables[dsn]))
        assert bv._row_sanity("scratch", "live")[0] is True

        tables["live"].append(("gold.reactor", 3))
        ok, detail = bv._row_sanity("scratch", "live")
        assert ok is False
        assert "gold.reactor" in detail


class TestParamRootOverride:
    def test_target_root_param_wins(self, tmp_path, monkeypatch):
        _events(monkeypatch)
        _toc_ok(monkeypatch)
        other = tmp_path / "elsewhere"
        create_backup(_DSN, output_dir=other, fmt="custom", _dry_run=True)
        # Policy points at an empty dir; the param must win.
        save_backup_policy(BackupPolicy(target_root=str(tmp_path / "empty")), state_dir=tmp_path)
        result = bv.run({"target_root": str(other)}, _ctx(tmp_path))
        assert result.ok
