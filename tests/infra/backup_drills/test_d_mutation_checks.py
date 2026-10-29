# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Mutation checks: break each safety property on purpose and prove a drill notices.

A drill suite that passes against broken code proves nothing.  Each test here removes ONE safety
property from the product (a monkeypatch that makes the protective step a no-op or does the
dangerous thing) and runs the real drill for that property, which must now FAIL.  The same drill
passes, unmutated, in its own module.

Properties covered: fallback-to-older (state, vault), refuse-when-all-bad, pre-restore-backup
required (state, vault), verify-after-restore (state, vault), atomic per-file writes,
external-head rollback detection, no-secret-in-output, retention-never-leaves-zero-verified,
the writer lock, no-oracle decryption errors, conflicts-never-overwritten, ledger-order "latest",
symlinks-never-replaced, the fallback is announced.
"""

from __future__ import annotations

import contextlib
import logging
import os

import pytest

from axiom.infra import backup_core as bc
from axiom.infra import state_snapshots as ss
from axiom.infra import vault_backup as vb
from axiom.infra.vault_backup import crypto as C
from axiom.infra.vault_backup import engine as vengine

from . import conftest as drill_conftest
from . import test_a_damaged_backups as A
from . import test_b_interrupted_and_hostile_restores as B
from . import test_c_keys_and_emergency_script as K


@pytest.fixture(autouse=True)
def _keep_the_matrix_clean():
    n = len(drill_conftest.TALLY)
    yield
    del drill_conftest.TALLY[n:]


def detected(drill, *args, **kwargs):
    """The drill must fail (an assertion, which includes DrillFailure) under the mutant."""
    with pytest.raises(AssertionError):
        drill(*args, **kwargs)


def newest_regardless(kind):
    """Mutant of selection: take the newest, never look at whether it verifies."""
    return bc.Selection(chosen=kind.candidates()[0] if kind.candidates() else None)


def test_mutant_fallback_to_older_is_gone_state(make, monkeypatch):
    monkeypatch.setattr(bc, "select_newest_intact", newest_regardless)
    detected(A.test_a_newest_backup_damaged_falls_back_to_the_next_that_verifies, make, "state", "bitflip",
             "restored-from-fallback")


def test_mutant_fallback_to_older_is_gone_vault(make, monkeypatch):
    monkeypatch.setattr(bc, "select_newest_intact", newest_regardless)
    detected(A.test_a_newest_backup_damaged_falls_back_to_the_next_that_verifies, make, "vault", "bitflip",
             "restored-from-fallback")


def test_mutant_a_bad_backup_is_used_when_all_are_bad(make, monkeypatch):
    monkeypatch.setattr(bc, "select_newest_intact", newest_regardless)
    monkeypatch.setattr(ss, "verify_snapshot", lambda store, sid, deep=True: ss.VerifyResult(sid, True))
    detected(A.test_b_every_backup_bad_refuses_lists_each_reason_and_the_ways_out, make, "state")


def test_mutant_restore_proceeds_without_a_safety_snapshot(make, monkeypatch):
    real = ss.create_snapshot

    def swallow(*a, **k):
        try:
            return real(*a, **k)
        except Exception:  # noqa: BLE001 -- the mutant: a failed safety net is ignored
            if str(k.get("reason", "")).startswith("pre-restore"):
                return ss.SnapshotResult(ok=True, snapshot_id=None)
            raise

    monkeypatch.setattr(ss, "create_snapshot", swallow)
    detected(B.test_j_without_a_safety_backup_the_restore_refuses_and_changes_nothing, make, "state")


def test_mutant_restore_proceeds_without_a_safety_backup_vault(make, monkeypatch):
    real = vengine.create_backup

    def swallow(*a, **k):
        try:
            return real(*a, **k)
        except Exception:  # noqa: BLE001
            return vengine.BackupResult(status="created", id=None)

    monkeypatch.setattr(vengine, "create_backup", swallow)
    detected(B.test_j_without_a_safety_backup_the_restore_refuses_and_changes_nothing, make, "vault")


def test_mutant_verify_after_restore_is_skipped_state(make, monkeypatch):
    monkeypatch.setattr(ss, "_landed", lambda target, sha: True)
    detected(B.test_k_state_a_dropped_write_is_caught_by_the_re_hash_and_the_original_is_kept, make,
             "existing-file")


def test_mutant_verify_after_restore_is_skipped_vault(make, monkeypatch):
    monkeypatch.setattr(vengine, "_landed", lambda after, item: True)
    detected(B.test_k_vault_a_value_that_did_not_land_is_reported_by_name, make)


def test_mutant_file_replace_is_not_atomic(make, monkeypatch):
    def torn_commit(tmp, dest, *, fsync_parent=True):
        data = tmp.read_bytes()
        with open(dest, "wb") as f:  # in place: the destination is torn for the whole window
            f.write(data[: max(1, len(data) // 2)])
            f.flush()
            bc.FAULTS.hit("commit.replace", path=str(dest), staged=str(tmp))
            f.write(data[max(1, len(data) // 2):])
        os.unlink(tmp)

    monkeypatch.setattr(bc, "commit_staged", torn_commit)
    detected(B.test_e_a_restore_killed_mid_way_leaves_each_file_old_or_new_and_rerunning_completes_it, make,
             "state", "commit.replace", 1)


def test_mutant_the_external_head_never_notices_a_rollback(make, monkeypatch):
    monkeypatch.setattr(bc.ExternalHead, "check", lambda self, root, ledger: [])
    detected(A.test_d_a_rolled_back_or_missing_ledger_is_detected_and_latest_is_refused, make, "state",
             "ledger-rolled-back")


def test_mutant_a_secret_reaches_the_log(make, monkeypatch):
    real = vengine._unlock_value

    def leaky(unlock):
        v = real(unlock)
        logging.getLogger("axiom.vault_backup").error("unlock=%s", v)
        return v

    monkeypatch.setattr(vengine, "_unlock_value", leaky)
    detected(A.test_a_newest_backup_damaged_falls_back_to_the_next_that_verifies, make, "vault", "bitflip",
             "restored-from-fallback")


def test_mutant_retention_deletes_everything(make, monkeypatch):
    from datetime import timedelta  # noqa: F401

    def delete_all(infos, policy, now, *, pressure=False):
        return bc.RetentionPlan(decisions=tuple(bc.Decision(i.id, "delete", ("mutant",)) for i in infos))

    monkeypatch.setattr(bc, "plan_retention", delete_all)
    detected(A.test_n_retention_that_asks_for_nothing_leaves_a_verified_backup_that_restores, make, "vault")


def test_mutant_the_writer_lock_does_nothing(make, monkeypatch):
    @contextlib.contextmanager
    def no_lock(lockfile, *, prepare=None, **k):
        if prepare is not None:
            prepare()  # (the mutant keeps the directory setup and drops only the mutual exclusion)
        yield

    monkeypatch.setattr(bc, "backup_lock", no_lock)
    detected(B.test_l_a_restore_while_a_backup_holds_the_lock_refuses_and_names_the_lock, make, "state")


def test_mutant_the_decryption_error_is_an_oracle(make, monkeypatch):
    real = C.open_archive
    h_holder = {}

    def oracle(data, *, unlock):
        try:
            return real(data, unlock=unlock)
        except C.DecryptionError:
            h = h_holder["h"]
            raise C.VaultBackupError("wrong code" if unlock != h.code else "archive damaged") from None

    class Spy:
        def __init__(self, inner):
            self.inner = inner

        def __call__(self, kind):
            h = self.inner(kind)
            h_holder["h"] = h
            return h

    monkeypatch.setattr(C, "open_archive", oracle)
    detected(K.test_i_wrong_code_and_damaged_archive_look_identical_so_the_error_is_no_oracle, Spy(make))


def test_mutant_conflicting_values_are_overwritten_without_asking(make, monkeypatch):
    real = vengine.restore

    def always_overwrite(*a, **k):
        return real(*a, **{**k, "overwrite": True})

    monkeypatch.setattr(vb, "restore", always_overwrite)
    detected(B.test_f_vault_a_different_value_already_there_is_left_alone_and_reported_until_overwrite, make)


def test_mutant_latest_follows_the_clock_not_the_order_taken(make, monkeypatch):
    def by_clock(store):
        def created(sid):
            return ss._created(store, sid)

        return sorted(store.ids(), key=lambda i: (created(i), i), reverse=True)

    monkeypatch.setattr(ss, "newest_first", by_clock)
    detected(A.test_m_latest_follows_the_order_taken_not_the_clock, make, monkeypatch, "state",
             "future-dated-middle")


def test_mutant_a_symlink_in_the_way_is_replaced_silently(make, monkeypatch, tmp_path):
    real = ss.diff_snapshot

    def no_conflicts(*a, **k):
        out = real(*a, **k)
        for d in out:
            if d.action == "conflict":
                d.action = "replace"
        return out

    monkeypatch.setattr(ss, "diff_snapshot", no_conflicts)
    (tmp_path / "mutant").mkdir()
    detected(B.test_f_state_a_symlink_in_the_way_is_a_reported_conflict_and_the_link_is_untouched, make,
             tmp_path / "mutant")


def test_mutant_the_fallback_is_not_announced(make, monkeypatch):
    monkeypatch.setattr(bc.Selection, "explain", lambda self, noun: [])
    detected(A.test_a_newest_backup_damaged_falls_back_to_the_next_that_verifies, make, "state", "bitflip",
             "restored-from-fallback")


def test_mutant_retention_prunes_on_a_quick_check_only(make, monkeypatch):
    """The state prune used to trust a quick verification; removing the deep re-check must be caught."""
    real = ss.verify_store

    def quick_only(store, *, deep=True):
        return real(store, deep=False)

    monkeypatch.setattr(ss, "verify_store", quick_only)
    detected(A.test_n_when_the_newest_are_corrupt_pruning_keeps_the_last_good_one, make, "state")
