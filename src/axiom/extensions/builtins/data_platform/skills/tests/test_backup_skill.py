# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``data.backup`` — policy-driven backup + prune + off-box leg.

Everything runs against the ``_dry_run`` seam and fake uploaders; no
pg_dump, no Postgres, no network.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from axiom.extensions.builtins.data_platform import _herald
from axiom.extensions.builtins.data_platform.database.backup_policy import (
    BackupPolicy,
    save_backup_policy,
)
from axiom.extensions.builtins.data_platform.database.backup_uploader import (
    UploadReceipt,
)
from axiom.extensions.builtins.data_platform.skills import backup as backup_skill
from axiom.infra.backup import create_backup
from axiom.infra.skills import SkillContext, SkillRegistry

_DSN = "postgresql://axiom:pw@localhost:5432/axiom_db"


def _ctx(tmp_path: Path) -> SkillContext:
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=tmp_path,
        logger=logging.getLogger("test.backup"),
    )


def _events(monkeypatch) -> list[tuple[str, str]]:
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        _herald,
        "publish_event",
        lambda intent, summary, **kw: captured.append((intent, summary)) or "rcpt-test",
    )
    return captured


class TestDsnResolution:
    def test_no_dsn_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DP1_RAG_DSN", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        result = backup_skill.run({"_dry_run": True}, _ctx(tmp_path))
        assert not result.ok
        assert any("DSN" in e for e in result.errors)

    def test_env_dsn_used(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DP1_RAG_DSN", _DSN)
        target = tmp_path / "backups"
        save_backup_policy(BackupPolicy(target_root=str(target)), state_dir=tmp_path)
        result = backup_skill.run({"_dry_run": True}, _ctx(tmp_path))
        assert result.ok


class TestBackupHappyPath:
    def test_creates_custom_format_artifact(self, tmp_path):
        target = tmp_path / "backups"
        save_backup_policy(BackupPolicy(target_root=str(target)), state_dir=tmp_path)
        result = backup_skill.run({"dsn": _DSN, "_dry_run": True}, _ctx(tmp_path))
        assert result.ok
        artifact = Path(result.value["artifact"])
        assert artifact.suffix == ".dump"
        assert artifact.parent == target
        assert result.value["size_bytes"] > 0
        assert result.value["fmt"] == "custom"

    def test_audit_receipt_in_actions(self, tmp_path):
        target = tmp_path / "backups"
        save_backup_policy(BackupPolicy(target_root=str(target)), state_dir=tmp_path)
        result = backup_skill.run({"dsn": _DSN, "_dry_run": True}, _ctx(tmp_path))
        assert any(a.startswith("audit-receipt:") for a in result.actions_taken)
        assert result.value["receipt"]

    def test_param_target_root_overrides_policy(self, tmp_path):
        save_backup_policy(
            BackupPolicy(target_root=str(tmp_path / "policy-root")),
            state_dir=tmp_path,
        )
        override = tmp_path / "override-root"
        result = backup_skill.run(
            {"dsn": _DSN, "target_root": str(override), "_dry_run": True},
            _ctx(tmp_path),
        )
        assert result.ok
        assert Path(result.value["artifact"]).parent == override

    def test_works_without_policy_file(self, tmp_path):
        """No persisted policy → safe defaults; manual backup still works."""
        override = tmp_path / "adhoc"
        result = backup_skill.run(
            {"dsn": _DSN, "target_root": str(override), "_dry_run": True},
            _ctx(tmp_path),
        )
        assert result.ok


class TestRetentionPrune:
    def test_prunes_to_retention_count(self, tmp_path):
        target = tmp_path / "backups"
        save_backup_policy(
            BackupPolicy(target_root=str(target), retention_count=2),
            state_dir=tmp_path,
        )
        # Pre-seed three older artifacts with distinct mtimes.
        for i, label in enumerate(["a", "b", "c"]):
            r = create_backup(_DSN, output_dir=target, label=label, fmt="custom", _dry_run=True)
            os.utime(r.backup_path, (1_000_000 + i, 1_000_000 + i))

        result = backup_skill.run({"dsn": _DSN, "_dry_run": True}, _ctx(tmp_path))
        assert result.ok
        assert result.value["pruned"] == 2
        remaining = list(target.glob("axiom-backup-*"))
        assert len(remaining) == 2
        # The just-created artifact survives.
        assert Path(result.value["artifact"]) in remaining

    def test_no_prune_under_retention(self, tmp_path):
        target = tmp_path / "backups"
        save_backup_policy(
            BackupPolicy(target_root=str(target), retention_count=14),
            state_dir=tmp_path,
        )
        result = backup_skill.run({"dsn": _DSN, "_dry_run": True}, _ctx(tmp_path))
        assert result.ok
        assert result.value["pruned"] == 0


class TestPolicyValidation:
    def test_invalid_policy_fails_closed(self, tmp_path):
        save_backup_policy(
            BackupPolicy(target_root=str(tmp_path / "b"), retention_count=0),
            state_dir=tmp_path,
        )
        result = backup_skill.run({"dsn": _DSN, "_dry_run": True}, _ctx(tmp_path))
        assert not result.ok
        assert any("retention_count" in e for e in result.errors)


class TestFailurePublishesHerald:
    def test_pg_dump_failure_emits_event(self, tmp_path, monkeypatch):
        events = _events(monkeypatch)
        target = tmp_path / "backups"
        save_backup_policy(BackupPolicy(target_root=str(target)), state_dir=tmp_path)

        from axiom.infra.backup import BackupResult

        def _failing(*args, **kwargs):
            return BackupResult(
                backup_path=target / "nope.dump",
                size_bytes=0,
                created_at="2026-07-11T00:00:00+00:00",
                database_url=_DSN,
                success=False,
                error="disk full",
            )

        monkeypatch.setattr(backup_skill, "create_backup", _failing)
        result = backup_skill.run({"dsn": _DSN}, _ctx(tmp_path))
        assert not result.ok
        assert tuple({e[0] for e in events}) == ("data.backup.failed",)
        assert any("disk full" in e for e in result.errors)


class TestOffboxLeg:
    def _policy(self, tmp_path) -> None:
        save_backup_policy(
            BackupPolicy(
                target_root=str(tmp_path / "backups"),
                offbox="box",
                box_folder_id="999",
            ),
            state_dir=tmp_path,
        )

    def test_upload_success(self, tmp_path, monkeypatch):
        self._policy(tmp_path)
        uploaded: list[tuple[Path, str]] = []

        class _FakeUploader:
            def upload(self, path, *, folder_id):
                uploaded.append((path, folder_id))
                return UploadReceipt(ok=True, remote_id="f-123", detail="fake")

        monkeypatch.setattr(backup_skill, "_make_uploader", lambda policy, params: _FakeUploader())
        result = backup_skill.run({"dsn": _DSN, "_dry_run": True}, _ctx(tmp_path))
        assert result.ok
        assert result.value["offbox_ok"] is True
        assert result.value["offbox_remote_id"] == "f-123"
        assert uploaded and uploaded[0][1] == "999"

    def test_upload_failure_fails_closed_and_alerts(self, tmp_path, monkeypatch):
        self._policy(tmp_path)
        events = _events(monkeypatch)

        class _FakeUploader:
            def upload(self, path, *, folder_id):
                return UploadReceipt(ok=False, detail="box 503")

        monkeypatch.setattr(backup_skill, "_make_uploader", lambda policy, params: _FakeUploader())
        result = backup_skill.run({"dsn": _DSN, "_dry_run": True}, _ctx(tmp_path))
        assert not result.ok
        assert result.value["offbox_ok"] is False
        assert tuple({e[0] for e in events}) == ("data.backup.failed",)
        # Local artifact still exists — the failure is the replica, not the dump.
        assert Path(result.value["artifact"]).exists()

    def test_offbox_none_never_builds_uploader(self, tmp_path, monkeypatch):
        save_backup_policy(
            BackupPolicy(target_root=str(tmp_path / "backups")),
            state_dir=tmp_path,
        )

        def _boom(policy, params):
            raise AssertionError("uploader must not be built when offbox='none'")

        monkeypatch.setattr(backup_skill, "_make_uploader", _boom)
        result = backup_skill.run({"dsn": _DSN, "_dry_run": True}, _ctx(tmp_path))
        assert result.ok
        assert result.value["offbox_ok"] is None


class TestRegistration:
    def test_backup_skills_registered(self):
        from axiom.extensions.builtins.data_platform import skills as data_skills

        registry = SkillRegistry()
        data_skills.bind(registry)
        assert registry.has("data.backup")
        assert registry.has("data.backup_validate")
