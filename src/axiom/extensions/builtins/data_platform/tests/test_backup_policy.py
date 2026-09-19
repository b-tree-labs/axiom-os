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


# ---------------------------------------------------------------------------
# Schema scoping is the wrong knob, and it fails silently.
#
# Found on a live node: a dump scoped to {bronze, gold, catalog, webapp,
# policy, ...} produced an 83 MB artifact that passed backup_exists,
# backup_fresh, backup_nonempty AND toc_parses — four green checks — and then
# failed to restore:
#
#   pg_restore: ERROR: relation "public.documents" does not exist
#   Command was: CREATE VIEW bronze.documents AS SELECT ... FROM public.documents;
#
# `bronze.documents` selects from `public`; all seven `gold` views select from
# `silver`. Both were excluded. Only the scratch restore caught it.
# ---------------------------------------------------------------------------


def test_the_policy_can_exclude_data_while_keeping_definitions():
    """The lever that makes a correct backup fit: objects restore, bulk is empty."""
    from axiom.extensions.builtins.data_platform.database.backup_policy import BackupPolicy

    policy = BackupPolicy(
        enabled=True,
        exclude_table_data=["public.reactor_timeseries_*", "silver.signals"],
    )
    assert policy.exclude_table_data == ["public.reactor_timeseries_*", "silver.signals"]
    # and scoping stays available, just not the default answer
    assert policy.schemas is None


def test_the_dump_timeout_is_configurable():
    """A 368 GB dump does not finish in ten minutes, and a dump killed by its
    own timeout must fail loudly rather than leave a truncated artifact."""
    from axiom.extensions.builtins.data_platform.database.backup_policy import BackupPolicy

    assert BackupPolicy().timeout_s == 600
    assert BackupPolicy(timeout_s=7200).timeout_s == 7200


def test_exclude_table_data_reaches_pg_dump_as_a_flag(tmp_path):
    """Verify the argv, not the intent."""
    import subprocess

    import axiom.infra.backup as backup_mod

    seen = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["timeout"] = kwargs.get("timeout")
        tmp_path.joinpath("x").write_bytes(b"PGDMP")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    original = backup_mod.subprocess.run
    backup_mod.subprocess.run = _fake_run
    try:
        backup_mod.create_backup(
            "postgresql://u@h/db",
            output_dir=tmp_path,
            fmt="custom",
            exclude_table_data=["public.big_a", "silver.big_b"],
            timeout_s=7200,
        )
    finally:
        backup_mod.subprocess.run = original

    assert "--exclude-table-data=public.big_a" in seen["cmd"]
    assert "--exclude-table-data=silver.big_b" in seen["cmd"]
    assert seen["timeout"] == 7200
    # scoping absent means no -n flags invented
    assert "-n" not in seen["cmd"]


# ---------------------------------------------------------------------------
# A field the dataclass has and the loader drops is worse than a missing field.
#
# `exclude_table_data` and `timeout_s` were added to BackupPolicy and NOT to
# `load_backup_policy`, which enumerates every field by hand. The policy file on
# the node set both; the loader returned None and 600. Arming the schedule would
# have dumped the whole 368 GB database against the old timeout and left a
# truncated artifact — behind a policy file that read exactly right.
#
# This guard is reflective on purpose: it compares the dataclass's fields to
# what a round trip actually preserves, so the NEXT field added cannot be
# forgotten the same way.
# ---------------------------------------------------------------------------


def test_every_policy_field_survives_a_round_trip(tmp_path):
    """Whatever the dataclass declares, the file must carry and the loader read."""
    import dataclasses

    from axiom.extensions.builtins.data_platform.database.backup_policy import (
        BackupPolicy,
        backup_policy_path,
        load_backup_policy,
        save_backup_policy,
    )

    # Values deliberately different from every default, so a field that is
    # silently dropped comes back as the default and fails loudly.
    written = BackupPolicy(
        enabled=True,
        schedule="15 3 * * *",
        validate_schedule="45 7 * * *",
        target_root="/tmp/not-the-default",
        retention_count=3,
        schemas=["alpha", "beta"],
        exclude_table_data=["public.huge", "silver.also_huge"],
        timeout_s=7200,
        validate_restore=False,
        scratch_dsn="postgresql://u@h/scratch",
        offbox="box",
        box_folder_id="123456",
    )
    save_backup_policy(written, state_dir=tmp_path)
    assert backup_policy_path(state_dir=tmp_path).exists()

    read = load_backup_policy(state_dir=tmp_path)
    assert read is not None

    dropped = [
        f.name
        for f in dataclasses.fields(BackupPolicy)
        if getattr(read, f.name) != getattr(written, f.name)
    ]
    assert not dropped, (
        f"the loader dropped {dropped} — a field the dataclass declares and the "
        "loader ignores is a setting that reads correctly on disk and does "
        "nothing, which is how a backup policy silently stops applying"
    )


def test_the_two_fields_that_were_actually_dropped(tmp_path):
    """Named explicitly, because these are the ones that would have hurt."""
    from axiom.extensions.builtins.data_platform.database.backup_policy import (
        BackupPolicy,
        load_backup_policy,
        save_backup_policy,
    )

    save_backup_policy(
        BackupPolicy(
            enabled=True,
            exclude_table_data=["public.reactor_timeseries_default"],
            timeout_s=3600,
        ),
        state_dir=tmp_path,
    )
    read = load_backup_policy(state_dir=tmp_path)
    assert read.exclude_table_data == ["public.reactor_timeseries_default"]
    assert read.timeout_s == 3600
