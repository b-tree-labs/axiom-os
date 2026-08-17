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
        }
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
        dropped: list[str] = []
        monkeypatch.setattr(bv, "_restore_into_scratch", lambda p, dsn: (True, "restored"))
        monkeypatch.setattr(bv, "_row_sanity", lambda scratch, live: (True, "12 user tables"))
        monkeypatch.setattr(bv, "_drop_scratch_objects", dropped.append)
        result = bv.run(
            {"validate_restore": True, "scratch_dsn": "postgresql://s/scratch"},
            _ctx(tmp_path),
        )
        assert result.ok
        assert _check(result, "restore_live")["status"] == "PASS"
        assert dropped == ["postgresql://s/scratch"]  # scratch always cleaned

    def test_scratch_restore_failure_fails_closed(self, tmp_path, monkeypatch):
        events = _events(monkeypatch)
        _toc_ok(monkeypatch)
        _seed_backup(tmp_path, age_hours=1.0)
        dropped: list[str] = []
        monkeypatch.setattr(bv, "_restore_into_scratch", lambda p, dsn: (False, "restore exploded"))
        monkeypatch.setattr(bv, "_drop_scratch_objects", dropped.append)
        result = bv.run(
            {"validate_restore": True, "scratch_dsn": "postgresql://s/scratch"},
            _ctx(tmp_path),
        )
        assert not result.ok
        assert _check(result, "restore_live")["status"] == "FAIL"
        assert [e[0] for e in events] == ["data.backup.validate_failed"]
        assert dropped == ["postgresql://s/scratch"]

    def test_row_sanity_failure_fails_closed(self, tmp_path, monkeypatch):
        _events(monkeypatch)
        _toc_ok(monkeypatch)
        _seed_backup(tmp_path, age_hours=1.0)
        monkeypatch.setattr(bv, "_restore_into_scratch", lambda p, dsn: (True, "restored"))
        monkeypatch.setattr(
            bv,
            "_row_sanity",
            lambda scratch, live: (False, "scratch restore produced zero user tables"),
        )
        monkeypatch.setattr(bv, "_drop_scratch_objects", lambda dsn: None)
        result = bv.run(
            {"validate_restore": True, "scratch_dsn": "postgresql://s/scratch"},
            _ctx(tmp_path),
        )
        assert not result.ok


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
