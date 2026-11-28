# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""PLINTH declares each table's storage rule, and an install's backup proves itself by a restore."""

from __future__ import annotations

import logging

from axiom.extensions.builtins.data_platform.database.backup_policy import (
    DEFAULT_SCRATCH_DB,
    BackupPolicy,
    load_backup_policy,
    save_backup_policy,
)
from axiom.extensions.builtins.data_platform.skills import storage_rules as sr
from axiom.infra.skills import SkillContext, SkillRegistry

TS = [
    {"engine": "hypertable", "schema": "store", "table": "events", "identity": ["k"],
     "rows_per_day": 1000, "compress_after": "7d"},
    {"schema": "store", "table": "daily", "granularity": "day"},  # partitions, no retain
]
LIMITS = [
    {"schema": "auth", "table": "identity_audit", "retain": "365d"},
    {"schema": "store", "table": "readings", "rederivable": True},
    {"table": "settings", "grows": False},
]


def _ctx(tmp_path):
    return SkillContext(registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("t"))


def test_rules_come_from_both_declarations():
    rules, gaps = sr.rules(TS, LIMITS)
    by = {r["table"]: r for r in rules}
    assert by["store.events"]["compress_after"] == "7d"
    assert by["auth.identity_audit"]["retain"] == "365d"
    assert by["store.readings"]["rederivable"] is True
    assert by["public.settings"]["grows"] is False
    assert any("store.daily" in g and "retain_periods" in g for g in gaps)
    assert any("store.readings" in g for g in gaps)  # declared, grows, no bound
    assert not any("settings" in g or "identity_audit" in g or "events" in g for g in gaps)


def test_the_skill_reports_the_backup_posture_and_an_unproven_backup_is_a_gap(tmp_path):
    save_backup_policy(BackupPolicy(enabled=True, exclude_table_data=["store.readings"],
                                    scratch_db=None), state_dir=tmp_path)
    res = sr.run({"_timeseries": [], "_limits": []}, _ctx(tmp_path))
    assert res.ok
    assert res.value["backup"]["exclude_table_data"] == ["store.readings"]
    assert any("never restores" in g for g in res.value["gaps"])


def test_an_installed_policy_restores_into_a_scratch_database_by_default(tmp_path):
    assert BackupPolicy().validate_restore and BackupPolicy().scratch_db == DEFAULT_SCRATCH_DB
    save_backup_policy(BackupPolicy(enabled=True), state_dir=tmp_path)
    assert load_backup_policy(state_dir=tmp_path).scratch_db == DEFAULT_SCRATCH_DB
    res = sr.run({"_timeseries": [], "_limits": []}, _ctx(tmp_path))
    assert res.value["backup"]["restore_target"] and not res.value["gaps"]


def test_a_policy_file_written_before_the_default_gets_it(tmp_path):
    save_backup_policy(BackupPolicy(enabled=True), state_dir=tmp_path)
    path = next(tmp_path.rglob("*.toml"))
    path.write_text("\n".join(line for line in path.read_text().splitlines()
                              if not line.startswith("scratch_db")) + "\n")
    assert load_backup_policy(state_dir=tmp_path).scratch_db == DEFAULT_SCRATCH_DB


def test_an_explicit_opt_out_survives_a_reload(tmp_path):
    save_backup_policy(BackupPolicy(enabled=True, scratch_db=None), state_dir=tmp_path)
    assert load_backup_policy(state_dir=tmp_path).scratch_db is None
