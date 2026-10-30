# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Rolling backup retention + restore — defense-in-depth for ``data.json``.

The atomic write (test_gate_failure_injection::TestAtomicSave) prevents a torn
file at write time; this suite proves the layer on top of it: every real change
leaves a timestamped backup, no-op writes do not, the history is pruned to a
bounded policy, and a chosen prior state can be restored — including when the
live ``data.json`` is corrupt or truncated, which is exactly when it matters.

Real files throughout (no mocks of the subject): ``save_program`` writes the
backups, ``snapshots`` prunes them, and the ``restore`` skill rolls back. Time
is injected so counts are deterministic.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from axiom.extensions.builtins.program import snapshots
from axiom.extensions.builtins.program.model import (
    ProgramData,
    ProgramError,
    load_program,
    save_program,
)
from axiom.extensions.builtins.program.skills import people, sync
from axiom.extensions.builtins.program.skills import restore as restore_skill
from axiom.infra.skills import SkillContext, SkillRegistry

NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)


def _edited(data: ProgramData, **item_one: Any) -> ProgramData:
    raw = copy.deepcopy(data.raw)
    raw["schedule"][0].update(item_one)
    return ProgramData(raw=raw)


# ---------------------------------------------------------------------------
# the stamp: filesystem-safe, sortable, round-trips
# ---------------------------------------------------------------------------


class TestStamp:
    def test_stamp_round_trips_through_a_filename(self):
        name = f"{snapshots.SNAPSHOT_PREFIX}{snapshots.snapshot_stamp(NOW)}{snapshots.SNAPSHOT_SUFFIX}"
        assert snapshots.parse_stamp(name) == NOW

    def test_stamp_has_no_colons_so_it_is_filesystem_safe(self):
        assert ":" not in snapshots.snapshot_stamp(NOW)

    def test_stamps_sort_chronologically_as_text(self):
        earlier = snapshots.snapshot_stamp(NOW - timedelta(days=3))
        later = snapshots.snapshot_stamp(NOW)
        assert earlier < later

    def test_a_collision_suffix_still_parses(self):
        stamp = snapshots.snapshot_stamp(NOW)
        assert snapshots.parse_stamp(f"data-{stamp}-1.json") == NOW

    def test_a_foreign_filename_is_not_one_of_ours(self):
        assert snapshots.parse_stamp("snapshot.json") is None
        assert snapshots.parse_stamp("data.json") is None
        assert snapshots.parse_stamp("data-not-a-stamp.json") is None


# ---------------------------------------------------------------------------
# a change writes a backup; a no-op does not
# ---------------------------------------------------------------------------


class TestChangeWritesABackup:
    def test_a_change_writes_exactly_one_backup(self, data_file: Path, tmp_path: Path):
        snaps = tmp_path / "snapshots"
        data = load_program(data_file)
        save_program(_edited(data, pct=77), data_file, now=NOW)
        kept = snapshots.list_stamped(snaps)
        assert len(kept) == 1
        # the backup is a byte-for-byte copy of what landed in data.json
        assert kept[0][0].read_text(encoding="utf-8") == data_file.read_text(encoding="utf-8")

    def test_a_noop_resave_writes_no_backup(self, data_file: Path, tmp_path: Path):
        snaps = tmp_path / "snapshots"
        data = load_program(data_file)
        # identical parsed content (the fixture is pretty-printed differently,
        # but the document is the same) → not a change → no backup.
        save_program(data, data_file, now=NOW)
        assert snapshots.list_stamped(snaps) == []

    def test_a_second_identical_write_adds_nothing(self, data_file: Path, tmp_path: Path):
        snaps = tmp_path / "snapshots"
        data = load_program(data_file)
        changed = _edited(data, pct=77)
        save_program(changed, data_file, now=NOW)
        save_program(changed, data_file, now=NOW + timedelta(hours=1))
        assert len(snapshots.list_stamped(snaps)) == 1  # second save was a no-op

    def test_two_distinct_changes_write_two_backups(self, data_file: Path, tmp_path: Path):
        snaps = tmp_path / "snapshots"
        data = load_program(data_file)
        save_program(_edited(data, pct=77), data_file, now=NOW)
        save_program(_edited(data, pct=78), data_file, now=NOW + timedelta(hours=1))
        assert len(snapshots.list_stamped(snaps)) == 2

    def test_first_creation_of_a_file_is_a_change(self, tmp_path: Path, data_dict):
        dpath = tmp_path / "data.json"  # does not exist yet
        save_program(ProgramData(raw=data_dict), dpath, now=NOW)
        assert len(snapshots.list_stamped(tmp_path / "snapshots")) == 1

    def test_snapshots_can_be_disabled_by_env(self, data_file: Path, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AXIOM_PROGRAM_SNAPSHOTS", "off")
        data = load_program(data_file)
        save_program(_edited(data, pct=77), data_file, now=NOW)
        assert snapshots.list_stamped(tmp_path / "snapshots") == []

    def test_the_change_detection_snapshot_json_is_separate(self, data_file: Path, tmp_path: Path):
        """save_program's backup history must never be confused with the
        change-detection snapshot.json (owned by _changelog). save_program
        writes only under snapshots/ and never a snapshot.json."""
        data = load_program(data_file)
        save_program(_edited(data, pct=77), data_file, now=NOW)
        assert (tmp_path / "snapshots").is_dir()
        assert not (tmp_path / "snapshot.json").exists()


# ---------------------------------------------------------------------------
# the write paths that feed save_program: mutation commit + sync
# ---------------------------------------------------------------------------


def _cli_ctx(state: Path) -> SkillContext:
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=state,
        logger=logging.getLogger("test.program.snapshots"),
        user_prompt=None,
        surface="cli",
    )


class TestTheWritePathsSnapshot:
    def test_a_mutation_commit_leaves_a_backup(self, tmp_path: Path, data_dict):
        state = tmp_path / "state"
        (state / "program").mkdir(parents=True)
        (state / "program" / "data.json").write_text(json.dumps(data_dict, indent=1), encoding="utf-8")
        ctx = _cli_ctx(state)

        result = people.add(
            {"principal": "@newbie:example-org", "lane": ["alpha"], "role": "Builder"}, ctx
        )
        assert result.ok, result.errors
        backups = snapshots.list_stamped(state / "program" / "snapshots")
        assert len(backups) >= 1
        # and the change-detection snapshot.json also exists, separately.
        assert (state / "program" / "snapshot.json").exists()

    def test_sync_from_a_distinct_source_leaves_a_backup(self, tmp_path: Path, data_dict):
        dpath = tmp_path / "data.json"
        dpath.write_text(json.dumps(data_dict, indent=1), encoding="utf-8")
        upstream = tmp_path / "upstream.json"
        changed = copy.deepcopy(data_dict)
        changed["schedule"][0]["pct"] = 42
        upstream.write_text(json.dumps(changed, indent=1), encoding="utf-8")

        ctx = _cli_ctx(tmp_path / "state")
        result = sync.run({"source": str(upstream), "data": str(dpath)}, ctx)
        assert result.ok, result.errors
        assert result.value["data_updated"] is True
        assert len(snapshots.list_stamped(tmp_path / "snapshots")) == 1
        assert load_program(dpath).item("i-one")["pct"] == 42


# ---------------------------------------------------------------------------
# the retention policy: keeps exactly the policy's set, bounds growth
# ---------------------------------------------------------------------------


class TestRetentionSelection:
    POLICY = snapshots.RetentionPolicy(recent_hours=24, daily_days=14, weekly_weeks=8)

    def test_keeps_exactly_the_policys_set(self):
        # Named instants relative to NOW, each placed in a known tier.
        A = NOW - timedelta(hours=1)    # recent        -> kept
        B = NOW - timedelta(hours=2)    # recent        -> kept
        C = NOW - timedelta(hours=25)   # day -1, newest -> kept (daily rep)
        D = NOW - timedelta(hours=30)   # day -1, older  -> pruned
        E = NOW - timedelta(days=3)     # day -3         -> kept (daily rep)
        F = NOW - timedelta(days=10)    # day -10        -> kept (daily rep)
        W1a = NOW - timedelta(days=20)             # weekly region, newest of its week -> kept
        W1b = NOW - timedelta(days=20, hours=2)    # same calendar day/week as W1a       -> pruned
        W2 = NOW - timedelta(days=30)   # a different ISO week -> kept (weekly rep)
        W3 = NOW - timedelta(days=50)   # a different ISO week -> kept (weekly rep)
        Z1 = NOW - timedelta(days=60)   # older than 8 weeks -> pruned
        Z2 = NOW - timedelta(days=120)  # older still         -> pruned

        stamps = [A, B, C, D, E, F, W1a, W1b, W2, W3, Z1, Z2]
        kept = snapshots.select_kept(stamps, NOW, self.POLICY)

        assert kept == {A, B, C, E, F, W1a, W2, W3}
        for pruned in (D, W1b, Z1, Z2):
            assert pruned not in kept

    def test_empty_history_keeps_nothing(self):
        assert snapshots.select_kept([], NOW, self.POLICY) == set()

    def test_the_single_newest_is_always_kept_even_under_an_aggressive_policy(self):
        aggressive = snapshots.RetentionPolicy(recent_hours=0, daily_days=0, weekly_weeks=0)
        old = NOW - timedelta(days=365)
        newest = NOW - timedelta(days=100)
        kept = snapshots.select_kept([old, newest], NOW, aggressive)
        assert newest in kept  # safety floor: never zero backups

    def test_pruning_on_disk_keeps_exactly_the_selected_set_and_bounds_growth(self, tmp_path: Path):
        snaps = tmp_path / "snapshots"
        # Seed ~3 months of backups, one every 6 hours, pruning on each write.
        start = NOW - timedelta(days=90)
        stamps: list[datetime] = []
        step = start
        while step <= NOW:
            stamps.append(step)
            snapshots.write_snapshot(snaps, "{}\n", now=step, policy=self.POLICY)
            step += timedelta(hours=6)

        survivors = {ts for _, ts in snapshots.list_stamped(snaps)}
        # incremental pruning converges on the same set a one-shot select would.
        assert survivors == snapshots.select_kept(stamps, NOW, self.POLICY)
        # bounded: last-24h (<=4 at a 6h cadence) + 14 daily + 8 weekly, well
        # under the ~360 writes.
        assert len(survivors) <= self.POLICY.recent_hours // 6 + self.POLICY.daily_days + self.POLICY.weekly_weeks + 2
        assert len(survivors) < 40
        assert max(stamps) in survivors   # newest kept
        assert min(stamps) not in survivors  # oldest pruned

    def test_prune_leaves_foreign_files_alone(self, tmp_path: Path):
        snaps = tmp_path / "snapshots"
        snaps.mkdir()
        (snaps / "README.txt").write_text("not a backup", encoding="utf-8")
        snapshots.write_snapshot(snaps, "{}\n", now=NOW - timedelta(days=200), policy=self.POLICY)
        snapshots.prune(snaps, NOW, self.POLICY)
        assert (snaps / "README.txt").exists()


# ---------------------------------------------------------------------------
# restore: recovers a chosen prior state, even from a corrupt live file
# ---------------------------------------------------------------------------


def _seed_states(state: Path, data_dict) -> tuple[Path, dict, dict]:
    """Two successive committed states, so snapshots/ holds a prior (v1) and
    the current (v2). Returns (data_path, v1, v2)."""
    (state / "program").mkdir(parents=True)
    dpath = state / "program" / "data.json"
    v1 = copy.deepcopy(data_dict)
    v1["program"]["name"] = "State One"
    v1["schedule"][0]["pct"] = 11
    v2 = copy.deepcopy(data_dict)
    v2["program"]["name"] = "State Two"
    v2["schedule"][0]["pct"] = 22
    save_program(ProgramData(raw=v1), dpath, now=NOW - timedelta(hours=2))
    save_program(ProgramData(raw=v2), dpath, now=NOW - timedelta(hours=1))
    return dpath, v1, v2


class TestRestore:
    def test_list_reports_each_backup_with_a_summary(self, tmp_path: Path, data_dict):
        state = tmp_path / "state"
        dpath, _v1, _v2 = _seed_states(state, data_dict)
        ctx = _cli_ctx(state)
        out = restore_skill.list_snapshots({}, ctx)
        assert out.ok
        assert out.value["count"] == 2
        first = out.value["snapshots"][0]
        assert first["readable"] is True
        assert first["people"] == len(data_dict["people"])
        assert first["items"] == len(data_dict["schedule"])
        assert first["lanes"] == len(data_dict["lanes"])

    def test_restore_rolls_back_to_a_chosen_prior_state(self, tmp_path: Path, data_dict):
        state = tmp_path / "state"
        dpath, v1, v2 = _seed_states(state, data_dict)
        assert load_program(dpath).program["name"] == "State Two"  # current
        ctx = _cli_ctx(state)

        backups = snapshots.list_stamped(state / "program" / "snapshots")
        oldest_name = backups[-1][0].name  # the v1 backup
        out = restore_skill.restore({"snapshot": oldest_name, "yes": True}, ctx)
        assert out.ok, out.errors

        restored = load_program(dpath)
        assert restored.program["name"] == "State One"
        assert restored.item("i-one")["pct"] == 11

    def test_restore_latest_picks_the_newest(self, tmp_path: Path, data_dict):
        state = tmp_path / "state"
        dpath, v1, v2 = _seed_states(state, data_dict)
        ctx = _cli_ctx(state)
        out = restore_skill.restore({"latest": True, "yes": True}, ctx)
        assert out.ok, out.errors
        assert load_program(dpath).program["name"] == "State Two"

    def test_restore_recovers_a_corrupt_live_file(self, tmp_path: Path, data_dict):
        """The headline guarantee: restore reads only the backups, so it works
        when the live data.json is truncated garbage that cannot be parsed."""
        state = tmp_path / "state"
        dpath, v1, v2 = _seed_states(state, data_dict)
        # Corrupt the authoritative file the way a torn write would.
        dpath.write_text('{"schema": "axiom.program/0.1", "progr', encoding="utf-8")
        with pytest.raises(ProgramError):
            load_program(dpath)  # the live file is genuinely unreadable now

        ctx = _cli_ctx(state)
        out = restore_skill.restore({"latest": True, "yes": True}, ctx)
        assert out.ok, out.errors
        # recovered to a valid, loadable state
        recovered = load_program(dpath)
        assert recovered.program["name"] == "State Two"

    def test_restore_recovers_an_empty_truncated_file(self, tmp_path: Path, data_dict):
        state = tmp_path / "state"
        dpath, v1, v2 = _seed_states(state, data_dict)
        dpath.write_text("", encoding="utf-8")  # zero-length truncation
        ctx = _cli_ctx(state)
        out = restore_skill.restore(
            {"snapshot": snapshots.list_stamped(state / "program" / "snapshots")[-1][0].name,
             "yes": True},
            ctx,
        )
        assert out.ok, out.errors
        assert load_program(dpath).program["name"] == "State One"

    def test_restore_headless_without_yes_is_refused(self, tmp_path: Path, data_dict):
        state = tmp_path / "state"
        dpath, v1, v2 = _seed_states(state, data_dict)
        ctx = _cli_ctx(state)  # user_prompt=None → headless
        out = restore_skill.restore({"latest": True}, ctx)
        assert not out.ok
        assert out.value["refused"] == restore_skill.UNCONFIRMED
        # and nothing changed
        assert load_program(dpath).program["name"] == "State Two"

    def test_restore_prompts_and_proceeds_on_confirmation(self, tmp_path: Path, data_dict):
        state = tmp_path / "state"
        dpath, v1, v2 = _seed_states(state, data_dict)
        ctx = SkillContext(
            registry=SkillRegistry(),
            state_dir=state,
            logger=logging.getLogger("test.program.snapshots"),
            user_prompt=lambda _p: "yes",
            surface="cli",
        )
        oldest = snapshots.list_stamped(state / "program" / "snapshots")[-1][0].name
        out = restore_skill.restore({"snapshot": oldest}, ctx)
        assert out.ok, out.errors
        assert load_program(dpath).program["name"] == "State One"

    def test_restore_declined_at_the_prompt_changes_nothing(self, tmp_path: Path, data_dict):
        state = tmp_path / "state"
        dpath, v1, v2 = _seed_states(state, data_dict)
        ctx = SkillContext(
            registry=SkillRegistry(),
            state_dir=state,
            logger=logging.getLogger("test.program.snapshots"),
            user_prompt=lambda _p: "n",
            surface="cli",
        )
        out = restore_skill.restore({"latest": True}, ctx)
        assert not out.ok
        assert out.value["refused"] == restore_skill.UNCONFIRMED
        assert load_program(dpath).program["name"] == "State Two"

    def test_restore_with_no_backups_refuses(self, tmp_path: Path, data_dict):
        state = tmp_path / "state"
        (state / "program").mkdir(parents=True)
        (state / "program" / "data.json").write_text(json.dumps(data_dict, indent=1), encoding="utf-8")
        ctx = _cli_ctx(state)
        out = restore_skill.restore({"latest": True, "yes": True}, ctx)
        assert not out.ok
        assert out.value["refused"] == "no_data"

    def test_restore_without_a_selector_lists_and_asks(self, tmp_path: Path, data_dict):
        state = tmp_path / "state"
        _seed_states(state, data_dict)
        ctx = _cli_ctx(state)
        out = restore_skill.restore({"yes": True}, ctx)
        assert not out.ok
        assert out.value["refused"] == "bad_request"
        assert "which backup" in out.errors[0].lower()

    def test_restore_refuses_an_unknown_selector(self, tmp_path: Path, data_dict):
        state = tmp_path / "state"
        _seed_states(state, data_dict)
        ctx = _cli_ctx(state)
        out = restore_skill.restore({"snapshot": "nope", "yes": True}, ctx)
        assert not out.ok
        assert out.value["refused"] == "bad_request"

    def test_restore_refuses_a_corrupt_backup(self, tmp_path: Path, data_dict):
        """A backup that is itself invalid is refused, not promoted over the
        live file — restore never makes things worse."""
        state = tmp_path / "state"
        dpath, v1, v2 = _seed_states(state, data_dict)
        snaps = state / "program" / "snapshots"
        bad = snaps / f"data-{snapshots.snapshot_stamp(NOW)}.json"
        bad.write_text("{ truncated", encoding="utf-8")
        ctx = _cli_ctx(state)
        out = restore_skill.restore({"snapshot": bad.name, "yes": True}, ctx)
        assert not out.ok
        assert out.value["refused"] == "no_data"
        # the live file is untouched
        assert load_program(dpath).program["name"] == "State Two"
