# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for defects the drills (or building them) exposed.  Each names the defect."""

from __future__ import annotations

import gzip
import os
import shutil
from datetime import UTC, datetime, timedelta

import pytest

from axiom.infra import backup as pg
from axiom.infra import backup_core as bc
from axiom.infra import state_snapshots as ss

from . import damage as D
from .harness import judge


@pytest.mark.parametrize("kind", ["state", "vault"])
def test_latest_never_means_the_backup_a_restore_took_to_undo_itself(make, kind):
    """DEFECT: a restore's own safety snapshot/backup is the newest thing in the store, so after ANY
    restore `latest` meant "the state the restore found" -- the damage -- and re-running a half-done
    restore would restore the damage over the repair."""
    h = make(kind)
    h.make_generations(3)
    h.break_target()
    first = h.restore_latest()
    assert first.rc == 0, first.out
    h.break_target()  # damaged again later
    before = h.digest()
    j = judge(h, h.restore_latest(), before)
    assert j.matched_gen == 3 and j.cls == "restored"


def test_state_verify_reports_an_emptied_manifest_instead_of_crashing(make):
    """DEFECT: `verify_store` re-read every manifest for its ledger cross-check and died with an
    unhandled SnapshotError on one that was unreadable -- the exact moment verify matters."""
    h = make("state")
    h.make_generations(3)
    D.zero_length(h.root / "snaps" / h.ids[1] / "manifest.json")
    res = h.verify()
    assert not res.crashed and res.rc != 0 and h.ids[1] in res.out and "unreadable" in res.out
    assert "Traceback" not in res.out


def test_a_forced_restore_from_a_rotted_blob_reports_that_file_instead_of_crashing(make):
    """DEFECT: a truncated blob raised EOFError/zlib.error out of the restore loop (not an OSError),
    so one bad file crashed the command with later files unrestored and nothing reported."""
    h = make("state")
    h.make_generations(3)
    D.truncate(h._blob(h.ids[2]))
    h.break_target()
    before = h.digest()
    res = h.restore_id(h.ids[2], force=True)
    j = judge(h, res, before)
    assert j.cls == "partial-restore-reported" and "settings.toml" in res.out
    assert (h.state / "memory" / "ledger.jsonl").exists()  # the intact files were still restored


def test_pg_dumps_keep_their_order_when_copied_without_preserving_mtimes(tmp_path):
    """DEFECT: `list_backups` ordered by mtime, and a plain `cp` to a second disk resets every mtime,
    so "newest" became whichever file was copied last."""
    src, dst = tmp_path / "primary", tmp_path / "copy"
    src.mkdir()
    dst.mkdir()
    for stamp in ("20261001_010101", "20261002_010101", "20261003_010101"):
        (src / f"axiom-backup-{stamp}.sql").write_text("-- PostgreSQL database dump complete\n")
    for p in sorted(src.iterdir(), reverse=True):  # copied newest-first: mtimes now run backwards
        shutil.copyfile(p, dst / p.name)
    assert [b.backup_path.name for b in pg.list_backups(dst)][0] == "axiom-backup-20261003_010101.sql"


def test_a_failed_snapshot_write_is_a_typed_error_not_a_raw_oserror(make):
    """DEFECT: a full disk surfaced as a bare OSError from deep inside the blob writer."""
    h = make("state")
    h.make_generations(1)
    h._write_gen(2)
    import errno

    with bc.FAULTS.inject() as plan:
        plan.fail("stage.write", exc=OSError(errno.ENOSPC, "No space left on device"))
        with pytest.raises(ss.DiskPressureError, match="untouched"):
            ss.create_snapshot(h.state, h.root, policy=h.policy, force=True, prune=False)


def test_stale_staged_files_from_a_killed_restore_are_never_snapshotted_and_are_swept(make):
    """DEFECT: a power cut leaves `.<name>.restore-*` beside the target; the next snapshot captured it as
    state, and nothing ever removed it."""
    h = make("state")
    h.make_generations(2)
    stale = h.state / ".settings.toml.restore-abc123"
    stale.write_text("half a file")
    old = (datetime.now(UTC) - timedelta(hours=3)).timestamp()
    os.utime(stale, (old, old))
    assert ss.classify(".settings.toml.restore-abc123").action == "exclude"
    assert ss._sweep_stale_restore_temps(h.state, ["settings.toml"]) == 1 and not stale.exists()
    fresh = h.state / ".settings.toml.restore-def456"
    fresh.write_text("a restore may still be writing this")
    assert ss._sweep_stale_restore_temps(h.state, ["settings.toml"]) == 0 and fresh.exists()


def test_the_pre_fix_gzip_error_types_are_one_answer_for_a_blob(make):
    h = make("state")
    h.make_generations(1)
    blob = h._blob(h.ids[0])
    blob.write_bytes(gzip.compress(b"x" * 100)[:15])  # truncated stream
    assert h.store.blob_ok(blob.name) is False
    with pytest.raises(ss.SnapshotError):
        h.store.read_blob(blob.name)
