# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The shared core: atomic writes, ledger + external head, locks, fault seam, selection."""

from __future__ import annotations

import errno
import json
import os
import stat
import threading
from datetime import timedelta

import pytest

from axiom.infra import backup_core as bc

# -- atomic publish ------------------------------------------------------------------


@pytest.fixture
def work(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


def test_atomic_publish_writes_private_bytes(work):
    tmp_path = work
    dest = tmp_path / "f"
    bc.atomic_publish(dest, b"new")
    assert dest.read_bytes() == b"new" and stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["f"]


@pytest.mark.parametrize("point", ["stage.write", "stage.fsync", "commit.replace"])
def test_a_failure_at_any_write_step_leaves_the_old_file_and_no_debris(work, point):
    tmp_path = work
    dest = tmp_path / "f"
    dest.write_bytes(b"old")
    with bc.FAULTS.inject() as plan:
        plan.fail(point)
        with pytest.raises(OSError):
            bc.atomic_publish(dest, b"new")
    assert dest.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["f"]


def test_a_simulated_power_cut_leaves_old_bytes_and_a_temp_file_like_a_real_one(work):
    tmp_path = work
    dest = tmp_path / "f"
    dest.write_bytes(b"old")
    with bc.FAULTS.inject() as plan:
        plan.crash("commit.replace")
        with pytest.raises(bc.SimulatedCrash):
            bc.atomic_publish(dest, b"new")
    assert dest.read_bytes() == b"old"
    assert any(p.name.startswith(".tmp-") for p in tmp_path.iterdir())  # nothing cleaned up: it was a crash


def test_a_dropped_write_is_silent_to_the_caller_and_changes_nothing(work):
    tmp_path = work
    dest = tmp_path / "f"
    dest.write_bytes(b"old")
    with bc.FAULTS.inject() as plan:
        plan.drop("commit.replace")
        bc.atomic_publish(dest, b"new")
    assert dest.read_bytes() == b"old"


def test_a_disk_quota_fails_with_enospc_and_keeps_the_old_file(work):
    tmp_path = work
    dest = tmp_path / "f"
    dest.write_bytes(b"old")
    with bc.FAULTS.inject() as plan:
        plan.quota(3)
        with pytest.raises(OSError) as e:
            bc.atomic_publish(dest, b"0123456789")
    assert e.value.errno == errno.ENOSPC and dest.read_bytes() == b"old"


# -- the fault seam is off by default and free ------------------------------------------


def test_faults_are_inactive_by_default_and_hit_is_a_no_op():
    assert bc.FAULTS.active is False
    assert bc.FAULTS.hit("anything", x=1) is False
    bc.FAULTS.account(10**12)  # no quota: nothing happens


def test_fault_plans_do_not_leak_between_uses_and_do_not_nest():
    with bc.FAULTS.inject() as plan:
        plan.fail("x")
        with pytest.raises(RuntimeError):
            with bc.FAULTS.inject():
                pass
    assert bc.FAULTS.active is False
    assert bc.FAULTS.hit("x") is False


def test_nth_and_predicate_select_the_failing_hit():
    with bc.FAULTS.inject() as plan:
        plan.fail("p", nth=2, when=lambda c: c.get("path", "").endswith("b"))
        assert bc.FAULTS.hit("p", path="a") is False
        assert bc.FAULTS.hit("p", path="b") is False  # 1st matching hit
        with pytest.raises(OSError):
            bc.FAULTS.hit("p", path="b")  # 2nd matching hit
        assert plan.hits["p"] == 3


# -- ledger and external head --------------------------------------------------------------


def _ledger(tmp_path, n=3):
    led = bc.HashChainLedger(tmp_path / "ledger.jsonl")
    for i in range(n):
        led.append("backup", f"b{i}", {"i": i})
    return led


def test_ledger_chain_verifies_and_head_is_the_last_hash(tmp_path):
    led = _ledger(tmp_path)
    chk = led.verify()
    assert chk.ok and chk.entries == 3 and chk.head == led.head()
    assert (tmp_path / "ledger.jsonl").stat().st_mode & 0o777 == 0o600


def test_editing_any_ledger_line_breaks_verification(tmp_path):
    led = _ledger(tmp_path)
    lines = led.path.read_text().splitlines()
    e = json.loads(lines[1])
    e["detail"] = {"i": 99}
    lines[1] = json.dumps(e)
    led.path.write_text("\n".join(lines) + "\n")
    assert not led.verify().ok


def test_removing_a_middle_line_breaks_the_chain(tmp_path):
    led = _ledger(tmp_path)
    lines = led.path.read_text().splitlines()
    del lines[1]
    led.path.write_text("\n".join(lines) + "\n")
    chk = led.verify()
    assert not chk.ok and any("chain does not link" in p for p in chk.problems)


def test_a_garbage_line_is_a_problem_not_a_crash(tmp_path):
    led = _ledger(tmp_path)
    with open(led.path, "a") as f:
        f.write("not json\n")
    assert not led.verify().ok


def test_external_head_detects_a_rolled_back_ledger(tmp_path):
    led = _ledger(tmp_path)
    head = bc.ExternalHead(tmp_path / "outside" / "head.json")
    head.record(tmp_path, led)
    older = led.path.read_text().splitlines()[:2]
    led.path.write_text("\n".join(older) + "\n")  # an older copy restored over the directory
    (problem,) = head.check(tmp_path, led)
    assert "ROLLBACK SUSPECTED" in problem and "3" in problem


def test_external_head_detects_a_deleted_ledger(tmp_path):
    led = _ledger(tmp_path)
    head = bc.ExternalHead(tmp_path / "outside" / "head.json")
    head.record(tmp_path, led)
    led.path.unlink()
    (problem,) = head.check(tmp_path, led)
    assert "LEDGER MISSING" in problem


def test_external_head_detects_a_rewritten_entry_of_the_same_length(tmp_path):
    led = _ledger(tmp_path)
    head = bc.ExternalHead(tmp_path / "outside" / "head.json")
    head.record(tmp_path, led)
    entries = led.entries()
    forged = dict(entries[-1], detail={"i": "other"})
    forged.pop("hash")
    forged["hash"] = bc.sha256_bytes(bc.canon(forged))
    entries[-1] = forged
    led.path.write_text("\n".join(bc.canon(e).decode() for e in entries) + "\n")
    (problem,) = head.check(tmp_path, led)
    assert "TAMPER SUSPECTED" in problem


def test_external_head_says_nothing_when_nothing_was_ever_recorded(tmp_path):
    led = _ledger(tmp_path)
    assert bc.ExternalHead(tmp_path / "nope.json").check(tmp_path, led) == []
    head = bc.ExternalHead(tmp_path / "head.json")
    head.record(tmp_path / "a", led)
    assert head.check(tmp_path / "other-root", led) == []  # keyed per root


def test_an_unreadable_head_record_is_reported_not_ignored(tmp_path):
    led = _ledger(tmp_path)
    (tmp_path / "head.json").write_text("{not json")
    (problem,) = bc.ExternalHead(tmp_path / "head.json").check(tmp_path, led)
    assert "unreadable" in problem


# -- manifests ------------------------------------------------------------------------------


def test_manifest_chain_round_trip_and_tamper():
    m = bc.build_manifest("prev-hash", {"files": [1, 2]})
    assert bc.check_manifest(m) == []
    m["body"]["files"] = [1, 2, 3]
    assert any("edited or bit-rotted" in p for p in bc.check_manifest(m))
    m2 = bc.build_manifest("p", {"a": 1})
    m2["prev"] = "other"
    assert any("chain broken" in p for p in bc.check_manifest(m2))
    assert bc.check_manifest({"body": "x"}) == ["manifest has no body"]


# -- lock -----------------------------------------------------------------------------------


class _Busy(RuntimeError):
    pass


def test_the_lock_serialises_and_reports_busy(tmp_path):
    lock = tmp_path / ".lock"
    held, release, got = threading.Event(), threading.Event(), []

    def holder():
        with bc.backup_lock(lock, busy=_Busy):
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(5)
    try:
        with pytest.raises(_Busy):
            with bc.backup_lock(lock, busy=_Busy, timeout=0.2):
                got.append(1)
    finally:
        release.set()
        t.join()
    assert got == []
    with bc.backup_lock(lock, busy=_Busy):  # free again
        pass


# -- disk -----------------------------------------------------------------------------------


def test_has_room_applies_the_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "free_bytes", lambda p: (1000, 10_000))
    assert bc.has_room(tmp_path, 100)[0]
    ok, why = bc.has_room(tmp_path, 100, min_free_bytes=950)
    assert not ok and "floor is 950" in why
    ok, _ = bc.has_room(tmp_path, 100, min_free_fraction=0.1)  # floor 1000
    assert not ok


def test_same_device_warning_text_and_none(tmp_path):
    w = bc.same_device_warning(tmp_path / "x", tmp_path, "Add a copy.", what="snapshot dir", of="the state dir")
    assert w and w.startswith("snapshot dir") and "same device as the state dir" in w and "Add a copy." in w
    assert bc.same_device_warning(tmp_path / "x", tmp_path / "missing" / "deeper", "") is not None
    assert bc.same_device(tmp_path, tmp_path / "does" / "not" / "exist")


# -- retention never leaves zero verified -----------------------------------------------------


def _info(i, hours_old, verified=True):
    from datetime import UTC, datetime

    return bc.BackupInfo(id=f"b{i:03d}", created_at=datetime(2026, 10, 1, tzinfo=UTC) - timedelta(hours=hours_old),
                         size_bytes=10, verified=verified)


def test_retention_misconfigured_to_keep_nothing_still_keeps_a_verified_backup():
    from datetime import UTC, datetime

    nothing = bc.RetentionPolicy(keep_last=0, keep_recent_hours=0, keep_daily=0, keep_weekly=0,
                                 keep_monthly=0, protect_newest=0, max_count=1, max_bytes=1)
    snaps = [_info(1, 1, False), _info(2, 2, False), _info(3, 3, False), _info(4, 5000, True)]
    plan = bc.plan_retention(snaps, nothing, datetime(2026, 10, 1, tzinfo=UTC))
    kept = {d.id for d in plan.keep}
    assert "b004" in kept  # the only verified one survives a policy that asks for nothing
    assert any(i.verified for i in snaps if i.id in kept)


# -- selection --------------------------------------------------------------------------------


class _Kind:
    noun = "thing"

    def __init__(self, bad):
        self.bad = bad

    def candidates(self):
        return ["c", "b", "a"]

    def check(self, bid):
        return [f"{bid} is {self.bad[bid]}"] if bid in self.bad else []


def test_selection_picks_the_newest_intact_and_explains_each_skip():
    sel = bc.select_newest_intact(_Kind({"c": "truncated", "b": "bit-flipped"}))
    assert sel.chosen == "a" and sel.fell_back
    lines = sel.explain("thing")
    assert "skipped thing c: c is truncated" in lines[0] and "skipped thing b" in lines[1]
    assert lines[-1].endswith("a")


def test_selection_with_everything_bad_chooses_nothing():
    sel = bc.select_newest_intact(_Kind({"a": "x", "b": "y", "c": "z"}))
    assert sel.chosen is None and [r.id for r in sel.rejected] == ["c", "b", "a"]


def test_selection_on_a_clean_newest_does_not_report_a_fallback():
    sel = bc.select_newest_intact(_Kind({}))
    assert sel.chosen == "c" and not sel.fell_back and sel.explain("thing") == []


def test_backup_kind_protocol_is_satisfied_by_the_real_kinds(tmp_path):
    from axiom.infra import state_snapshots as ss

    assert isinstance(ss.SnapshotKind(ss.Store(tmp_path)), bc.BackupKind)
    assert isinstance(_Kind({}), bc.BackupKind)


def test_utc_helpers_round_trip():
    now = bc.utcnow()
    assert abs((bc.parse_iso(bc.iso(now)) - now).total_seconds()) < 1e-3
    assert os.name  # keep import used


# -- later additions: rollback evidence cannot be laundered; repeat faults; directory diagnosis ----------


def test_the_recorded_head_never_moves_backwards_so_a_rollback_cannot_be_laundered(tmp_path):
    led = _ledger(tmp_path, 3)
    head = bc.ExternalHead(tmp_path / "outside" / "head.json")
    head.record(tmp_path, led)
    lines = led.path.read_text().splitlines()[:2]  # rolled back to 2 entries
    led.path.write_text("\n".join(lines) + "\n")
    led.append("backup", "b9", {})  # life goes on after the rollback: entry 3 is now a DIFFERENT entry
    head.record(tmp_path, led)  # recording after more appends must not erase the evidence
    led.append("backup", "b10", {})
    head.record(tmp_path, led)
    (problem,) = head.check(tmp_path, led) or ["none"]
    assert "TAMPER SUSPECTED" in problem or "ROLLBACK SUSPECTED" in problem


def test_recording_a_longer_ledger_advances_the_head(tmp_path):
    led = _ledger(tmp_path, 2)
    head = bc.ExternalHead(tmp_path / "head.json")
    head.record(tmp_path, led)
    led.append("backup", "b3", {})
    head.record(tmp_path, led)
    assert head.check(tmp_path, led) == []
    assert json.loads((tmp_path / "head.json").read_text())["roots"][str(tmp_path.resolve())]["seq"] == 3


def test_a_repeating_fault_models_a_disk_that_stays_full():
    with bc.FAULTS.inject() as plan:
        plan.fail("p", nth=2, repeat=True)
        assert bc.FAULTS.hit("p") is False
        for _ in range(3):
            with pytest.raises(OSError):
                bc.FAULTS.hit("p")


def test_ledger_check_names_the_first_bad_entry_so_the_earlier_ones_stay_trusted(tmp_path):
    led = _ledger(tmp_path, 4)
    lines = led.path.read_text().splitlines()
    e = json.loads(lines[2])
    e["prev"] = "0" * 64
    lines[2] = json.dumps(e)
    led.path.write_text("\n".join(lines) + "\n")
    chk = led.verify()
    assert not chk.ok and chk.first_bad == 3


def test_directory_problem_says_which_kind_of_wrong(tmp_path):
    d = tmp_path / "d"
    assert "does not exist" in bc.directory_problem(d, "snapshot directory")
    d.write_text("x")
    assert "not a directory" in bc.directory_problem(d)
    d.unlink()
    d.mkdir()
    assert bc.directory_problem(d) is None
    d.chmod(0)
    try:
        assert "permission denied" in bc.directory_problem(d)
    finally:
        d.chmod(0o700)
