# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Drills E, F, G, J, K, L: a restore that is killed half-way, a target in a worse state than the
backup, a full disk, a safety backup that cannot be made, a write that never lands, two things
at once.  Faults are injected at named steps (``backup_core.FAULTS``), never by timing."""

from __future__ import annotations

import errno
import os
import threading
import time
from pathlib import Path

import pytest

from axiom.infra import backup_core as bc
from axiom.infra import state_snapshots as ss

from . import damage as D
from .conftest import record
from .harness import judge


def _restore_phase(c) -> bool:
    """A write belonging to the restore itself (a staged ``.restore-`` file), not to a backup."""
    return ".restore-" in str(c.get("path", "")) + str(c.get("staged", ""))


def _vault_file_write(c) -> bool:
    return str(c.get("path", "")).endswith("private.pem") or str(c.get("directory", "")).endswith("identity")


def _in_restore(kind):
    return _restore_phase if kind == "state" else (lambda c: True)


# ---------------------------------------------------------------------------------------------
# E. a restore killed half-way: every file is old or new, re-running finishes, the safety backup undoes
# ---------------------------------------------------------------------------------------------

CRASH_POINTS = (
    [("state", p, n) for p, n in [("restore.file", 1), ("restore.file", 2), ("restore.file", 3), ("stage.write", 1),
                                  ("stage.write", 3), ("stage.fsync", 2), ("commit.replace", 1), ("commit.replace", 3)]]
    + [("vault", p, n) for p, n in [("restore.file", 1), ("restore.file", 2), ("restore.file", 3), ("stage.write", 1),
                                    ("stage.fsync", 1), ("commit.replace", 1)]]
)


@pytest.mark.parametrize(("kind", "point", "nth"), CRASH_POINTS, ids=[f"{k}-{p}-{n}" for k, p, n in CRASH_POINTS])
def test_e_a_restore_killed_mid_way_leaves_each_file_old_or_new_and_rerunning_completes_it(make, kind, point, nth):
    h = make(kind)
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    when = None if point == "restore.file" else (_restore_phase if kind == "state" else _vault_file_write)
    with bc.FAULTS.inject() as plan:
        plan.crash(point, nth=nth, when=when)
        killed = h.restore_latest()
    assert killed.killed, f"the fault at {point}#{nth} never fired: {killed.out}"
    mid = judge(h, killed, before)  # every key must be what it was or what a backup holds
    assert mid.cls == "killed"
    # (a power cut leaves its staged temp file behind; it must be harmless, so the rerun may see it)
    again = record("E", h, f"killed-at-{point}-{nth}-then-rerun",
                   judge(h, h.restore_latest(), mid.after, allow_debris=True))
    assert again.cls == "restored" and again.matched_gen == 3


@pytest.mark.parametrize("kind", ["state", "vault"])
def test_e_the_safety_backup_taken_before_a_killed_restore_lets_you_undo(make, kind):
    h = make(kind)
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    with bc.FAULTS.inject() as plan:
        plan.crash("restore.file", nth=2)
        h.restore_latest()
    if kind == "state":
        pre = next(i for i in h.store.ids() if i not in h.ids)
        res = h.restore_id(pre)
        assert res.rc == 0, res.out
        assert (h.state / "settings.toml").read_text() == "DAMAGED"  # the file as it was before the restore
    else:
        rows = h.vb.list_backups(h.root)
        pre = next(r["id"] for r in rows if r["reason"].startswith("pre-restore"))
        res = h.restore_id(pre, overwrite=True)
        assert res.rc == 0, res.out
        assert set(before) <= set(h.digest())
        assert all(h.digest()[k] == v for k, v in before.items())  # the vault as it was before


@pytest.mark.parametrize("kind", ["state", "vault"])
def test_e_a_write_that_fails_part_way_is_reported_as_a_partial_restore(make, kind):
    h = make(kind)
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    with bc.FAULTS.inject() as plan:
        if kind == "state":
            plan.fail("stage.write", nth=2, when=_restore_phase, exc=OSError(errno.EIO, "Input/output error"))
        else:
            plan.fail("restore.file", nth=2, exc=OSError(errno.EIO, "Input/output error"))
        res = h.restore_latest()
    j = record("E", h, "write-fails-mid-restore", judge(h, res, before))
    assert j.cls == "partial-restore-reported" and "Input/output error" in res.out
    # exactly one item failed; the rest are restored
    changed = [k for k in j.after if j.after[k] != before.get(k)]
    assert 0 < len(changed) < len(h.gen_digests[3])


# ---------------------------------------------------------------------------------------------
# F. the restore target is in a worse state than the backup
# ---------------------------------------------------------------------------------------------


def test_f_state_files_newer_than_the_snapshot_are_replaced_only_with_the_original_kept(make):
    h = make("state")
    h.make_generations(3)
    edited = h.state / "settings.toml"
    edited.write_text("MY NEWER LOCAL EDIT")
    future = time.time() + 86400 * 30
    os.utime(edited, (future, future))
    before = h.digest()
    res = h.restore_latest()
    j = record("F", h, "target-files-newer", judge(h, res, before))
    assert j.cls == "restored" and edited.read_text() == "[gen]\nn = 3\n"
    aside = list(h.state.glob("settings.toml.pre-restore-*"))
    assert len(aside) == 1 and aside[0].read_text() == "MY NEWER LOCAL EDIT"
    assert "kept previous settings.toml" in res.out


def test_f_state_a_symlink_in_the_way_is_a_reported_conflict_and_the_link_is_untouched(make, tmp_path):
    h = make("state")
    h.make_generations(3)
    outside = tmp_path / "outside-target.txt"
    outside.write_text("not ours to touch")
    (h.state / "settings.toml").unlink()
    (h.state / "settings.toml").symlink_to(outside)
    (h.state / "memory" / "ledger.jsonl").unlink()
    before = h.digest()
    res = h.restore_latest()
    j = record("F", h, "target-symlink", judge(h, res, before))
    assert j.cls == "partial-restore-reported", res.out
    assert (h.state / "settings.toml").is_symlink() and outside.read_text() == "not ours to touch"
    assert "settings.toml" in res.out and "symlink" in res.out
    assert (h.state / "memory" / "ledger.jsonl").exists()  # the rest was restored


def test_f_state_a_symlink_is_replaced_as_a_link_only_with_force_and_kept_aside(make, tmp_path):
    h = make("state")
    h.make_generations(3)
    outside = tmp_path / "outside-target.txt"
    outside.write_text("not ours to touch")
    (h.state / "settings.toml").unlink()
    (h.state / "settings.toml").symlink_to(outside)
    before = h.digest()
    res = h.restore_latest(force=True)
    j = judge(h, res, before)
    assert j.cls == "restored", res.out
    assert not (h.state / "settings.toml").is_symlink()
    assert outside.read_text() == "not ours to touch"  # the file the link pointed at was never written
    aside = list(h.state.glob("settings.toml.pre-restore-*"))
    assert len(aside) == 1 and aside[0].is_symlink()


def test_f_state_a_read_only_directory_fails_only_its_own_files_and_says_which(make):
    h = make("state")
    h.make_generations(3)
    h.break_target()
    (h.state / "memory").mkdir(exist_ok=True)
    D.read_only_dir(h.state / "memory")
    before = h.digest()
    try:
        res = h.restore_latest()
        j = record("F", h, "target-dir-read-only", judge(h, res, before))
    finally:
        os.chmod(h.state / "memory", 0o700)
    assert j.cls == "partial-restore-reported" and "memory/ledger.jsonl" in res.out
    assert (h.state / "settings.toml").read_text() == "[gen]\nn = 3\n"


def test_f_state_a_directory_where_a_file_belongs_is_a_conflict_not_a_crash(make):
    h = make("state")
    h.make_generations(3)
    (h.state / "settings.toml").unlink()
    (h.state / "settings.toml").mkdir()
    (h.state / "memory" / "ledger.jsonl").unlink()
    before = h.digest()
    res = h.restore_latest()
    j = record("F", h, "target-directory-in-the-way", judge(h, res, before))
    assert j.cls == "partial-restore-reported" and (h.state / "settings.toml").is_dir()


def test_f_state_partially_present_target_is_completed_and_a_wrong_mode_is_fixed(make):
    h = make("state")
    h.make_generations(3)
    (h.state / "memory" / "ledger.jsonl").unlink()
    os.chmod(h.state / "identity" / "node.json", 0o644)
    before = h.digest()
    j = record("F", h, "target-partially-present", judge(h, h.restore_latest(), before))
    assert j.cls == "restored" and j.matched_gen == 3
    assert (h.state / "identity" / "node.json").stat().st_mode & 0o777 == 0o600


def test_f_vault_a_different_value_already_there_is_left_alone_and_reported_until_overwrite(make):
    h = make("vault")
    h.make_generations(3)
    h.break_target()
    h.store.set("alpha-token", b"a-newer-value-set-after-the-backup", provider="guided")
    before = h.digest()
    res = h.restore_latest()
    j = record("F", h, "conflicting-value-present", judge(h, res, before))
    assert j.cls == "partial-restore-reported" and "alpha-token" in res.out and "LEFT ALONE" in res.out
    assert j.after["alpha-token"] == before["alpha-token"]  # untouched
    res2 = h.restore_latest(overwrite=True)
    j2 = judge(h, res2, j.after)
    assert j2.cls == "restored" and j2.matched_gen == 3


def test_f_vault_a_read_only_directory_fails_only_the_key_file_and_says_so(make):
    h = make("vault")
    h.make_generations(3)
    h.break_target()
    (h.state / "identity").mkdir(exist_ok=True)
    D.read_only_dir(h.state / "identity")
    before = h.digest()
    try:
        res = h.restore_latest()
        j = record("F", h, "target-dir-read-only", judge(h, res, before))
    finally:
        os.chmod(h.state / "identity", 0o700)
    assert j.cls == "partial-restore-reported" and "file:identity/private.pem" in res.out


def test_f_vault_a_symlinked_key_file_is_a_conflict_and_the_link_target_is_never_written(make, tmp_path):
    h = make("vault")
    h.make_generations(3)
    h.break_target()
    outside = tmp_path / "outside-key.pem"
    outside.write_text("someone else's key file")
    (h.state / "identity").mkdir(exist_ok=True)
    (h.state / "identity" / "private.pem").symlink_to(outside)
    before = h.digest()
    res = h.restore_latest()
    j = record("F", h, "target-symlink", judge(h, res, before))
    assert j.cls == "partial-restore-reported" and outside.read_text() == "someone else's key file"
    assert (h.state / "identity" / "private.pem").is_symlink()


# ---------------------------------------------------------------------------------------------
# G. the disk fills up
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["state", "vault"])
def test_g_disk_full_before_the_safety_backup_refuses_with_the_target_untouched(make, kind):
    h = make(kind)
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    with bc.FAULTS.inject() as plan:
        plan.quota(10)
        res = h.restore_latest()
    j = record("G", h, "disk-full-before-safety-backup", judge(h, res, before))
    assert j.cls == "refused-safely"
    assert "safety" in res.out.lower() or "pre-restore" in res.out.lower() or "No space" in res.out


@pytest.mark.parametrize("kind", ["state", "vault"])
def test_g_disk_full_during_the_restore_is_a_reported_partial_restore(make, kind):
    h = make(kind)
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    full = OSError(errno.ENOSPC, "No space left on device")
    with bc.FAULTS.inject() as plan:
        if kind == "state":
            plan.fail("stage.write", nth=2, when=_restore_phase, exc=full, repeat=True)
        else:
            plan.fail("restore.file", nth=2, exc=full, repeat=True)
        res = h.restore_latest()
    j = record("G", h, "disk-full-during-restore", judge(h, res, before))
    assert j.cls == "partial-restore-reported" and "No space left" in res.out
    assert res.rc != 0


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and p.name != ".lock" and "/tmp/" not in p.as_posix()}


@pytest.mark.parametrize("kind", ["state", "vault"])
def test_g_disk_full_during_a_backup_leaves_every_existing_backup_byte_identical(make, kind):
    h = make(kind)
    h.make_generations(2)
    snapshot_of_store = _tree_bytes(h.root)
    if kind == "state":
        h._write_gen(3)
        expected_errors: tuple = (ss.SnapshotError,)
    else:
        h._set_gen(3)
        expected_errors = (h.vb.VaultBackupError,)
    with bc.FAULTS.inject() as plan:
        plan.quota(40)
        with pytest.raises(expected_errors) as e:
            if kind == "state":
                ss.create_snapshot(h.state, h.root, policy=h.policy, force=True, prune=False)
            else:
                h.vb.create_backup(h.state, force=True)
    assert "untouched" in str(e.value) or "nothing was" in str(e.value).lower()
    after = _tree_bytes(h.root)
    # every file that existed is byte-identical; the only additions allowed are content-addressed blobs
    # (orphans, harmless, garbage-collected) -- no new snapshot, archive, manifest or ledger line
    assert all(after.get(p) == data for p, data in snapshot_of_store.items())
    assert all(p.startswith("blobs/") for p in set(after) - set(snapshot_of_store))
    assert not h.debris()
    ver = h.verify()
    assert ver.rc == 0, ver.out
    from .conftest import TALLY

    TALLY.append(("G", kind, "disk-full-during-backup", "refused-safely"))


# ---------------------------------------------------------------------------------------------
# J. the safety backup before a restore cannot be made: refuse, unless explicitly forced
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["state", "vault"])
def test_j_without_a_safety_backup_the_restore_refuses_and_changes_nothing(make, kind):
    h = make(kind)
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    with bc.FAULTS.inject() as plan:
        plan.fail("backup.publish", exc=OSError(errno.ENOSPC, "No space left on device"))
        res = h.restore_latest()
    j = record("J", h, "safety-backup-fails", judge(h, res, before))
    assert j.cls == "refused-safely"
    assert "safety" in res.out.lower() and ("--force" in res.out or "--skip-pre-backup" in res.out)


@pytest.mark.parametrize("kind", ["state", "vault"])
def test_j_with_the_explicit_override_the_restore_proceeds_and_says_there_is_no_undo(make, kind):
    h = make(kind)
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    with bc.FAULTS.inject() as plan:
        plan.fail("backup.publish", exc=OSError(errno.ENOSPC, "No space left on device"))
        res = h.restore_latest(**({"force": True} if kind == "state" else {"skip_pre_backup": True}))
    j = record("J", h, "safety-backup-fails-forced", judge(h, res, before))
    assert j.cls == "restored" and j.matched_gen == 3
    if kind == "state":
        assert "no pre-restore snapshot" in res.out


# ---------------------------------------------------------------------------------------------
# K. verify-after-restore catches a write that did not land
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["existing-file", "missing-file"])
def test_k_state_a_dropped_write_is_caught_by_the_re_hash_and_the_original_is_kept(make, target):
    h = make("state")
    h.make_generations(3)
    h.break_target()
    (h.state / "identity").mkdir()
    victim = "settings.toml" if target == "existing-file" else "memory/ledger.jsonl"
    before = h.digest()
    with bc.FAULTS.inject() as plan:
        plan.drop("commit.replace", when=lambda c: str(c["path"]).endswith(victim))
        res = h.restore_latest()
    j = record("K", h, f"dropped-write-{target}", judge(h, res, before))
    assert j.cls == "partial-restore-reported"
    assert victim in res.out and "did not land" in res.out
    assert j.after.get(victim) == before.get(victim)  # exactly what it was
    assert sum(1 for k in j.after if j.after[k] != before.get(k)) >= 1  # the other files were restored


def test_k_vault_a_value_that_did_not_land_is_reported_by_name(make):
    h = make("vault")
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    with bc.FAULTS.inject() as plan:
        plan.drop("restore.file", when=lambda c: c["path"] == "alpha-token")
        res = h.restore_latest()
    j = record("K", h, "dropped-write", judge(h, res, before))
    assert j.cls == "partial-restore-reported" and "re-verification failed for alpha-token" in res.out


# ---------------------------------------------------------------------------------------------
# L. concurrent backup / restore / restore
# ---------------------------------------------------------------------------------------------


def _hold(lock_ctx):
    held, release = threading.Event(), threading.Event()

    def run():
        with lock_ctx:
            held.set()
            release.wait(10)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert held.wait(10)
    return release, t


@pytest.mark.parametrize("kind", ["state", "vault"])
def test_l_a_restore_while_a_backup_holds_the_lock_refuses_and_names_the_lock(make, kind):
    h = make(kind)
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    lock = h.store.locked() if kind == "state" else h.vb.engine.BackupStore(h.root).locked()
    release, t = _hold(lock)
    try:
        res = h.restore_latest()
    finally:
        release.set()
        t.join()
    j = record("L", h, "restore-while-backup-holds-the-lock", judge(h, res, before))
    assert j.cls == "refused-safely" and ("lock" in res.out.lower() or "holds" in res.out.lower()
                                          or "running" in res.out.lower())


def test_l_two_restores_at_once_into_one_state_dir_never_both_write(make):
    h = make("state")
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    release, t = _hold(bc.backup_lock(h.root / ".restore.lock", busy=RuntimeError, prepare=h.store.ensure))
    try:
        res = h.restore_latest()
    finally:
        release.set()
        t.join()
    j = record("L", h, "two-restores-at-once", judge(h, res, before))
    assert j.cls == "refused-safely" and "another restore" in res.out


@pytest.mark.parametrize("kind", ["state", "vault"])
def test_l_a_real_race_between_two_restores_ends_consistent(make, kind):
    h = make(kind)
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    barrier, results = threading.Barrier(2), []
    h.capture = False  # overlapping stdout redirects would tangle; the verdict below needs only rc and state

    def one():
        barrier.wait()
        results.append(h.restore_latest())

    threads = [threading.Thread(target=one) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert len(results) == 2
    classes = []
    for r in results:
        # the second to arrive may be refused (busy) or find nothing left to do; neither may crash
        assert not r.crashed and not r.hung and "Traceback" not in r.out
        classes.append("ok" if r.rc == 0 else "refused")
    final = h.digest()
    assert h.matches(final, 3), f"final state is not the newest backup: {classes}"
    assert "ok" in classes
    assert not h.debris()
    from .conftest import TALLY

    TALLY.append(("L", kind, "real-race-two-restores", "restored"))
    del before
