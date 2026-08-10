# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the persisted :class:`BackupPolicy` (database backup config).

The policy is TOML-persisted in the extension's state dir (same model as
``ConnectorConfig``), validated with fail-closed messaging, and its
schedule strings must parse into PULSE cadences.
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.data_platform.database.backup_policy import (
    BackupPolicy,
    backup_policy_path,
    cadence_for,
    load_backup_policy,
    save_backup_policy,
    validate_policy,
)


class TestDefaults:
    def test_defaults_are_safe(self):
        p = BackupPolicy()
        assert p.enabled is False
        assert p.target_root == "~/.axi/backups"
        assert p.retention_count == 14
        assert p.schemas is None  # whole DB
        assert p.offbox == "none"
        assert p.box_folder_id is None

    def test_default_schedules_parse_as_cadences(self):
        p = BackupPolicy()
        assert cadence_for(p.schedule).kind in ("cron", "interval")
        assert cadence_for(p.validate_schedule).kind in ("cron", "interval")


class TestRoundTrip:
    def test_save_then_load(self, tmp_path: Path):
        p = BackupPolicy(
            enabled=True,
            schedule="0 2 * * *",
            validate_schedule="30 6 * * *",
            target_root="/natura/axiom-data/backups",
            retention_count=7,
            schemas=["rag", "memory"],
            offbox="box",
            box_folder_id="123456789",
        )
        path = save_backup_policy(p, state_dir=tmp_path)
        assert path == backup_policy_path(state_dir=tmp_path)
        assert path.exists()
        assert load_backup_policy(state_dir=tmp_path) == p

    def test_round_trip_whole_db(self, tmp_path: Path):
        p = BackupPolicy(enabled=True)
        save_backup_policy(p, state_dir=tmp_path)
        loaded = load_backup_policy(state_dir=tmp_path)
        assert loaded is not None
        assert loaded.schemas is None
        assert loaded.enabled is True

    def test_load_missing_returns_none(self, tmp_path: Path):
        assert load_backup_policy(state_dir=tmp_path) is None


class TestValidation:
    def test_valid_policy_has_no_errors(self):
        assert validate_policy(BackupPolicy()) == []

    def test_retention_count_must_be_positive(self):
        errors = validate_policy(BackupPolicy(retention_count=0))
        assert any("retention_count" in e for e in errors)

    def test_offbox_must_be_known(self):
        errors = validate_policy(BackupPolicy(offbox="s3"))
        assert any("offbox" in e for e in errors)

    def test_offbox_box_requires_folder_id(self):
        errors = validate_policy(BackupPolicy(offbox="box"))
        assert any("box_folder_id" in e for e in errors)

    def test_bad_schedule_string_is_an_error(self):
        errors = validate_policy(BackupPolicy(schedule="whenever"))
        assert any("schedule" in e for e in errors)

    def test_bad_validate_schedule_string_is_an_error(self):
        errors = validate_policy(BackupPolicy(validate_schedule="not a cadence"))
        assert any("validate_schedule" in e for e in errors)

    def test_empty_target_root_is_an_error(self):
        errors = validate_policy(BackupPolicy(target_root=""))
        assert any("target_root" in e for e in errors)


class TestCadenceStrings:
    def test_cron_five_field(self):
        c = cadence_for("0 2 * * *")
        assert c.kind == "cron"
        assert c.cron == "0 2 * * *"

    def test_cron_shortcut(self):
        assert cadence_for("@daily").kind == "cron"

    def test_iso8601_interval(self):
        c = cadence_for("PT6H")
        assert c.kind == "interval"
        assert int(c.interval.total_seconds()) == 6 * 3600
