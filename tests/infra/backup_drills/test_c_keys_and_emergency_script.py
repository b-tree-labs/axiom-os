# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Drills I and O.

I. A wrong or lost passphrase / recovery code: one generic error that is the same whether the
   code is wrong or the archive is damaged (no oracle), nothing changes, and the output says what
   the real options are.
O. The emergency script (``scripts/axi-restore.py``) for the failures above, run the way a broken
   install forces: ``python -I`` with a shadow ``axiom`` package that raises on import.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path

import pytest

from axiom.infra import backup_core as bc
from axiom.infra.vault_backup import crypto as C

from . import damage as D
from .conftest import record
from .harness import judge
from .kinds import StateHarness

# ---------------------------------------------------------------------------------------------
# I. keys
# ---------------------------------------------------------------------------------------------


def _norm(text: str, ids) -> str:
    for i in ids:
        text = text.replace(i, "<id>")
    return re.sub(r"\s+", " ", text).strip()


def test_i_a_wrong_recovery_code_is_one_generic_error_with_the_real_options(make):
    h = make("vault")
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    res = h.restore_latest(code_file=h.wrong_code_file)
    j = record("I", h, "wrong-recovery-code", judge(h, res, before))
    assert j.cls == "refused-safely"
    assert C.GENERIC_FAILURE in res.out
    for real_option in ("key-info", "second copy", "re-issue", "key-init"):
        assert real_option in res.out, real_option


def test_i_wrong_code_and_damaged_archive_look_identical_so_the_error_is_no_oracle(make):
    """If a wrong code and a damaged-but-recorded archive read differently, the message tells an
    attacker which one they have.  Both must produce the same words."""
    h = make("vault")
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    wrong_code = judge(h, h.restore_latest(code_file=h.wrong_code_file), before).result.out

    # damage the ciphertext of EVERY archive but keep the ledger consistent (so only decryption can tell)
    for bid in h.ids:
        a = h._archive(bid)
        D.bit_flip(a, 0.98)

    def fix(entries):
        for e in entries:
            if e["event"] == "backup":
                e["detail"]["sha256"] = bc.sha256_bytes(h._archive(e["id"]).read_bytes())
        return entries

    D.rewrite_ledger(h.root / "ledger.jsonl", fix, rehash=True)
    (h.state / "secrets" / "vault-backup-head.json").unlink()  # (the ledger edit above is deliberate; no head tamper noise)
    damaged = judge(h, h.restore_latest(), before).result.out
    assert _norm(wrong_code, h.ids) == _norm(damaged, h.ids)


def test_i_a_passphrase_backup_does_not_open_with_the_recovery_code_and_says_the_same_thing(make):
    h = make("vault")
    h.make_generations(1, wrong_key_newest=True)  # the only backup is passphrase-encrypted
    h.break_target()
    before = h.digest()
    res = h.restore_latest()
    j = record("I", h, "passphrase-backup-opened-with-recovery-code", judge(h, res, before))
    assert j.cls == "refused-safely" and C.GENERIC_FAILURE in res.out


def test_i_a_lost_code_with_no_terminal_says_what_that_means_and_changes_nothing(make):
    h = make("vault")
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    res = h._cli(["restore", "--apply"])  # no --code-file, and the test has no terminal to ask in
    j = record("I", h, "no-code-available", judge(h, res, before))
    assert j.cls == "refused-safely" and "recovery code" in res.out and "key-info" in res.out


@pytest.mark.parametrize("how", ["wrong-code", "lost-code"])
def test_i_the_emergency_script_is_just_as_generic_and_extracts_nothing(make, how):
    h = make("vault")
    h.make_generations(3)
    res, extracted = h.script_restore_latest(code_file=h.wrong_code_file if how == "wrong-code" else Path("/nonexistent"))
    assert res.rc != 0 and not extracted
    assert C.GENERIC_FAILURE in res.out if how == "wrong-code" else "cannot read" in res.out
    assert "try another copy" in res.out.lower() or "re-issued" in res.out or how == "lost-code"
    record("I", h, f"script-{how}", judge(h, res, {}, after=extracted))


# ---------------------------------------------------------------------------------------------
# O. the emergency script, for each failure where the main CLI is unusable
# ---------------------------------------------------------------------------------------------

STATE_HOWS = sorted(StateHarness.hows)
VAULT_SCRIPT_HOWS = ["bitflip", "truncate", "zero", "manifest-edit", "wrong-version", "foreign-key"]


@pytest.mark.parametrize("how", STATE_HOWS)
def test_o_state_script_falls_back_past_a_damaged_newest_snapshot(make, how):
    h = make("state")
    h.make_generations(3)
    h.damage(h.newest(), how)
    h.break_target()
    before = h.digest()
    res, after = h.script_restore_latest()
    j = record("O", h, f"script-newest-{how}", judge(h, res, before, after=after))
    assert j.cls == "restored-from-fallback" and j.matched_gen == 2


def test_o_state_script_with_every_snapshot_bad_refuses_and_lists_each_reason(make):
    h = make("state")
    h.make_generations(3)
    for bid in h.ids:
        h.damage(bid, "bitflip")
    h.break_target()
    before = h.digest()
    res, after = h.script_restore_latest()
    j = record("O", h, "script-all-bad", judge(h, res, before, after=after, must_mention=tuple(h.ids)))
    assert j.cls == "refused-safely" and "second copy" in res.out


@pytest.mark.parametrize("how", ["missing", "is-file", "permission-denied", "unmounted"])
def test_o_state_script_names_an_unusable_snapshot_location(make, how):
    h = make("state")
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    words = {"missing": "does not exist", "is-file": "not a directory", "permission-denied": "permission denied",
             "unmounted": "does not exist"}[how]
    snaps = h.root
    if how == "missing":
        shutil.rmtree(h.root)
    elif how == "is-file":
        D.replace_with_file(h.root)
    elif how == "permission-denied":
        D.make_unreadable(h.root)
    else:
        snaps = Path("/Volumes") / f"axiom-drill-{uuid.uuid4().hex}" / "snapshots"
    try:
        res, after = h.script_restore_latest(snapshots=snaps)
        j = record("O", h, f"script-location-{how}", judge(h, res, before, after=after, must_mention=(words,)))
    finally:
        if how == "permission-denied":
            D.make_readable(h.root)
    assert j.cls == "refused-safely"


def test_o_state_script_restores_from_a_second_copy(make):
    h = make("state")
    h.make_generations(3, second_copy=True)
    h.break_target()
    before = h.digest()
    shutil.rmtree(h.root)
    res, after = h.script_restore_latest(snapshots=h.second)
    j = record("O", h, "script-second-copy", judge(h, res, before, after=after))
    assert j.cls == "restored" and j.matched_gen == 3


@pytest.mark.parametrize("how", ["ledger-deleted", "ledger-rolled-back"])
def test_o_state_script_refuses_latest_when_the_ledger_is_missing_or_rolled_back(make, how):
    h = make("state")
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    if how == "ledger-deleted":
        (h.root / "ledger.jsonl").unlink()
    else:
        lines = (h.root / "ledger.jsonl").read_text().splitlines()
        (h.root / "ledger.jsonl").write_text("\n".join(lines[:-1]) + "\n")
    res, after = h.script_restore_latest()
    j = record("O", h, f"script-{how}", judge(h, res, before, after=after, must_mention=("--force",)))
    assert j.cls == "refused-safely" and ("LEDGER MISSING" in res.out or "ROLLBACK SUSPECTED" in res.out)


@pytest.mark.parametrize("nth", [1, 2, 3])
def test_o_state_script_killed_mid_restore_is_old_or_new_per_file_and_rerunning_finishes(make, nth):
    h = make("state")
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    res, after = h.script_restore_latest(driver=("crash", str(nth), "-"))
    assert res.killed, res.out
    mid = judge(h, res, before, after=after)
    assert mid.cls == "killed"
    res2, after2 = h.script_restore_latest()
    j = record("O", h, f"script-killed-at-{nth}-then-rerun", judge(h, res2, mid.after, after=after2,
                                                                    allow_debris=True))
    assert j.cls == "restored" and j.matched_gen == 3


def test_o_state_script_reports_a_write_that_did_not_land(make):
    h = make("state")
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    res, after = h.script_restore_latest(driver=("drop", "0", "settings.toml"))
    j = record("O", h, "script-dropped-write", judge(h, res, before, after=after))
    assert j.cls == "partial-restore-reported" and "settings.toml" in res.out and "did not land" in res.out
    assert after["settings.toml"] == before["settings.toml"]


def test_o_state_script_on_a_disk_that_cannot_hold_a_file_reports_a_partial_restore(make):
    h = make("state")
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    res, after = h.script_restore_latest(driver=("fsize", "20", "-"))  # no file may exceed 20 bytes
    j = record("O", h, "script-disk-full", judge(h, res, before, after=after))
    assert j.cls == "partial-restore-reported" and "PARTIAL RESTORE" in res.out


def test_o_state_script_leaves_a_symlink_alone_and_says_so(make, tmp_path):
    h = make("state")
    h.make_generations(3)
    outside = tmp_path / "outside.txt"
    outside.write_text("not ours")
    (h.state / "settings.toml").unlink()
    (h.state / "settings.toml").symlink_to(outside)
    (h.state / "memory" / "ledger.jsonl").unlink()
    before = h.digest()
    res, after = h.script_restore_latest()
    j = record("O", h, "script-symlink-conflict", judge(h, res, before, after=after))
    assert j.cls == "partial-restore-reported" and outside.read_text() == "not ours"
    assert (h.state / "settings.toml").is_symlink() and "symlink" in res.out


def test_o_state_script_restores_onto_a_fresh_machine(make, tmp_path):
    h = make("state")
    h.make_generations(3)
    fresh = tmp_path / "fresh-machine"
    (fresh / "state").mkdir(parents=True)
    copied = fresh / "snapshots"
    D.copy_tree(h.root, copied)
    res, after = h.script_restore_latest(snapshots=copied, state=fresh / "state")
    assert res.rc == 0, res.out
    for rel in ("settings.toml", "memory/ledger.jsonl", "identity/node.json"):
        assert (fresh / "state" / rel).read_bytes() == (h.state / rel).read_bytes()


def test_o_state_script_refuses_a_newer_schema_naming_the_version(make):
    h = make("state")
    h.make_generations(1)
    h.damage(h.newest(), "wrong-version")
    h.break_target()
    before = h.digest()
    res, after = h.script_restore_latest()
    j = record("O", h, "script-newer-schema", judge(h, res, before, after=after, must_mention=("newer",)))
    assert j.cls == "refused-safely"


# -- vault ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("how", VAULT_SCRIPT_HOWS)
def test_o_vault_script_opens_the_newest_archive_that_opens(make, how):
    h = make("vault")
    h.make_generations(3, wrong_key_newest=(how == "foreign-key"))
    if how != "foreign-key":
        h.damage(h.newest(), how)
    res, extracted = h.script_restore_latest()
    assert res.rc == 0, res.out
    assert extracted == {k: v for k, v in h.gen_digests[2].items()}
    assert "skipped" in res.out and h.newest() in res.out and "using the newest backup that opens" in res.out
    record("O", h, f"script-newest-{how}", judge(h, res, {}, after=extracted))


def test_o_vault_script_with_every_archive_bad_says_so_and_extracts_nothing(make):
    h = make("vault")
    h.make_generations(3)
    for bid in h.ids:
        h.damage(bid, "bitflip")
    res, extracted = h.script_restore_latest()
    j = record("O", h, "script-all-bad", judge(h, res, {}, after=extracted, must_mention=tuple(h.ids)))
    assert j.cls == "refused-safely" and not extracted and "second copy" in res.out


@pytest.mark.parametrize("how", ["missing", "is-file", "permission-denied"])
def test_o_vault_script_names_an_unusable_backup_location(make, how):
    h = make("vault")
    h.make_generations(3)
    words = {"missing": "does not exist", "is-file": "not a vault backup", "permission-denied": "permission denied"}[how]
    if how == "missing":
        shutil.rmtree(h.root)
    elif how == "is-file":
        D.replace_with_file(h.root)
    else:
        D.make_unreadable(h.root)
    try:
        res, extracted = h.script_restore_latest()
        j = record("O", h, f"script-location-{how}", judge(h, res, {}, after=extracted, must_mention=(words,)))
    finally:
        if how == "permission-denied":
            D.make_readable(h.root)
    assert j.cls == "refused-safely"


def test_o_vault_script_restores_from_a_second_copy_directory(make):
    h = make("vault")
    h.make_generations(3, second_copy=True)
    shutil.rmtree(h.root)
    res, extracted = h.script_restore_latest(source=h.second)
    assert res.rc == 0 and extracted == h.gen_digests[3], res.out
    record("O", h, "script-second-copy", judge(h, res, {}, after=extracted))


def test_o_vault_script_works_on_a_fresh_machine_with_nothing_but_python_and_the_archives(make, tmp_path):
    h = make("vault")
    h.make_generations(3)
    copied = tmp_path / "usb-stick" / "vault-backups"
    D.copy_tree(h.root, copied)
    shutil.rmtree(h.root)
    shutil.rmtree(h.state / "secrets", ignore_errors=True)  # no recipient record, no head record
    res, extracted = h.script_restore_latest(source=copied)
    assert res.rc == 0 and extracted == h.gen_digests[3], res.out
    assert os.stat(h.sandbox / "extracted").st_mode & 0o777 == 0o700
    record("O", h, "script-fresh-machine", judge(h, res, {}, after=extracted))
