# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Drills A-D, H, M, N: damaged backups, missing or hostile locations, rolled-back ledgers, fresh
machines, wrong clocks and a retention policy that asks for too much.

Each drill is one cell of the matrix {state snapshot, vault backup, pg dump} x scenario.  The
expected OUTCOME class is part of the parametrization; ``judge`` fails the drill outright for a
crash, a hang, a leaked secret, a torn file or silently wrong data.
"""

from __future__ import annotations

import os
import shutil
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from axiom.infra import backup_core as bc
from axiom.infra import state_snapshots as ss

from . import damage as D
from .conftest import record
from .harness import judge
from .kinds import PgHarness, StateHarness, VaultHarness

KINDS = ("state", "vault", "pg")

# ---------------------------------------------------------------------------------------------
# A. the newest backup is damaged: `restore latest` uses the next newest that verifies, says so
# ---------------------------------------------------------------------------------------------

CELLS_A = [(k, how, "restored-from-fallback")
           for k, H in (("state", StateHarness), ("vault", VaultHarness), ("pg", PgHarness))
           for how in sorted(H.hows)]


@pytest.mark.parametrize(("kind", "how", "expect"), CELLS_A, ids=[f"{k}-{h}" for k, h, _ in CELLS_A])
def test_a_newest_backup_damaged_falls_back_to_the_next_that_verifies(make, kind, how, expect):
    h = make(kind)
    h.make_generations(3, wrong_key_newest=(how == "foreign-key"))
    if how != "foreign-key":
        h.damage(h.newest(), how)
    h.break_target()
    before = h.digest()
    j = record("A", h, f"newest-{how}", judge(h, h.restore_latest(), before))
    assert j.cls == expect and j.matched_gen == 2
    if kind != "pg":  # the damaged one is NAMED with a reason, not just skipped
        assert h.newest() in j.result.out


@pytest.mark.parametrize("kind", ["state", "vault"])
def test_a_a_damaged_newest_backup_is_never_silently_used_even_when_the_older_ones_are_fine(make, kind):
    h = make(kind)
    h.make_generations(3)
    h.damage(h.newest(), "bitflip")
    h.break_target()
    before = h.digest()
    j = judge(h, h.restore_latest(), before)
    assert not h.matches(j.after, 3)  # the bad generation's content is nowhere in the target


# ---------------------------------------------------------------------------------------------
# B. every backup is bad: a clear refusal listing each reason and what to try next
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_b_every_backup_bad_refuses_lists_each_reason_and_the_ways_out(make, kind):
    h = make(kind)
    h.make_generations(3)
    for bid in h.ids:
        h.damage(bid, "bitflip")
    h.break_target()
    before = h.digest()
    res = h.restore_latest()
    j = record("B", h, "all-bad", judge(h, res, before, must_mention=tuple(h.ids) + ("second copy",)))
    assert j.cls == "refused-safely"
    if kind != "pg":
        assert "axi-restore.py" in res.out  # the emergency script is named as the next resort
    assert j.after == before


# ---------------------------------------------------------------------------------------------
# C. the backup location is gone, renamed, a file, unreadable, unmounted; a second copy saves the day
# ---------------------------------------------------------------------------------------------


def _break_location(h, how: str) -> None:
    root = h.root
    if how == "missing":
        shutil.rmtree(root)
    elif how == "renamed":
        root.rename(root.with_name(root.name + "-renamed"))
    elif how == "is-file":
        D.replace_with_file(root)
    elif how == "permission-denied":
        D.make_unreadable(root)
    elif how == "unmounted":
        pass  # handled by pointing at a path under a mount point that does not exist
    else:  # pragma: no cover
        raise ValueError(how)


WORDS = {"missing": "does not exist", "renamed": "does not exist", "is-file": "not a directory",
         "permission-denied": "permission denied", "unmounted": "does not exist"}


@pytest.mark.parametrize("how", ["missing", "renamed", "is-file", "permission-denied", "unmounted"])
@pytest.mark.parametrize("kind", KINDS)
def test_c_backup_location_unusable_refuses_with_the_real_reason(make, kind, how):
    h = make(kind)
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    try:
        if how == "unmounted":
            gone = Path("/Volumes") / f"axiom-drill-{uuid.uuid4().hex}" / h.root.name
            res = h.restore_latest(to=gone)
        else:
            _break_location(h, how)
            res = h.restore_latest()
        j = record("C", h, f"location-{how}", judge(h, res, before, must_mention=(WORDS[how],)))
        assert j.cls == "refused-safely"
    finally:
        if how == "permission-denied" and h.root.exists():
            D.make_readable(h.root)


@pytest.mark.parametrize("kind", KINDS)
def test_c_the_second_copy_is_used_when_the_primary_is_gone(make, kind):
    h = make(kind)
    h.make_generations(3, second_copy=True)
    h.break_target()
    before = h.digest()
    shutil.rmtree(h.root)
    if kind == "vault":  # the configured second copy is found automatically, and the report says so
        res = h.restore_latest()
    else:  # state/pg: the operator names it; the first attempt must have said how
        first = h.restore_latest()
        assert "--from" in first.out or "second copy" in first.out
        res = h.restore_latest(to=h.second)
    j = record("C", h, "second-copy-used", judge(h, res, before, fallback_ok_without_note=(kind != "vault")))
    assert j.matched_gen == 3 and j.cls in ("restored", "restored-from-fallback")
    if kind == "vault":
        assert j.cls == "restored-from-fallback" and "USED THE SECOND COPY" in res.out


# ---------------------------------------------------------------------------------------------
# D. the ledger is deleted or rolled back: the head recorded outside the directory notices
# ---------------------------------------------------------------------------------------------


def _rollback(h, kind: str) -> None:
    """The ledger as it was before the newest backup was made (an older copy restored over it)."""
    ledger = h.root / "ledger.jsonl"
    lines = ledger.read_text().splitlines()
    ledger.write_text("\n".join(lines[:-1]) + "\n")


@pytest.mark.parametrize("how", ["ledger-deleted", "ledger-rolled-back"])
@pytest.mark.parametrize("kind", ["state", "vault"])
def test_d_a_rolled_back_or_missing_ledger_is_detected_and_latest_is_refused(make, kind, how):
    h = make(kind)
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    if how == "ledger-deleted":
        (h.root / "ledger.jsonl").unlink()
    else:
        _rollback(h, kind)
    ver = h.verify()
    assert ver.rc != 0 and ("ROLLBACK SUSPECTED" in ver.out or "LEDGER MISSING" in ver.out), ver.out
    res = h.restore_latest()
    j = record("D", h, how, judge(h, res, before, must_mention=("--force",) if kind == "state" else ()))
    assert j.cls == "refused-safely"
    assert ("ROLLBACK SUSPECTED" in res.out or "LEDGER MISSING" in res.out or "ledger" in res.out.lower())


@pytest.mark.parametrize("kind", ["state"])
def test_d_with_the_ledger_gone_an_explicitly_named_backup_still_restores_with_a_warning(make, kind):
    h = make(kind)
    h.make_generations(3)
    h.break_target()
    before = h.digest()
    (h.root / "ledger.jsonl").unlink()
    j = judge(h, h.restore_id(h.ids[1]), before, explicit=True)
    assert j.cls == "restored" and j.matched_gen == 2
    assert "LEDGER MISSING" in j.result.out
    j2 = record("D", h, "ledger-deleted-explicit-id", j)
    assert j2.matched_gen == 2


@pytest.mark.parametrize("kind", ["state", "vault"])
def test_d_a_broken_ledger_chain_only_distrusts_what_comes_after_the_break(make, kind):
    h = make(kind)
    h.make_generations(3)
    if kind == "vault":
        h.damage(h.newest(), "chain-break")
    else:  # the ledger line of the NEWEST snapshot is edited, nothing re-hashed
        def edit(entries):
            entries[-1]["detail"]["reason"] = "forged"
            return entries

        D.rewrite_ledger(h.root / "ledger.jsonl", edit, rehash=False)
    h.break_target()
    before = h.digest()
    res = h.restore_latest()
    j = record("D", h, "ledger-chain-broken-at-newest", judge(h, res, before))
    assert j.cls == "restored-from-fallback" and j.matched_gen == 2 and "ledger" in res.out


# ---------------------------------------------------------------------------------------------
# H. a fresh machine; a backup from a newer format
# ---------------------------------------------------------------------------------------------


def test_h_state_restores_onto_an_empty_state_dir_with_another_home_and_hostname(make, monkeypatch, tmp_path):
    h = make("state")
    h.make_generations(3)
    # a new machine: empty state dir elsewhere, another HOME and hostname, only the copied snapshots
    fresh = tmp_path / "fresh-machine"
    (fresh / "home").mkdir(parents=True)
    (fresh / "state").mkdir()
    copied = fresh / "snapshots"
    D.copy_tree(h.root, copied)
    monkeypatch.setenv("HOME", str(fresh / "home"))
    monkeypatch.setattr("socket.gethostname", lambda: "a-different-host")
    res = ss.restore_snapshot(fresh / "state", copied, "latest", apply=True, _services=lambda _s: [])
    assert res.ok and not res.not_restored and res.snapshot_id == h.ids[-1], res.errors
    for rel in ("settings.toml", "memory/ledger.jsonl", "identity/node.json"):
        assert (fresh / "state" / rel).read_bytes() == (h.state / rel).read_bytes()


def test_h_vault_restores_onto_a_fresh_machine_with_only_the_archives_and_the_code(make, monkeypatch, tmp_path):
    h = make("vault")
    h.make_generations(3)
    fresh_state = tmp_path / "fresh" / "state"
    (tmp_path / "fresh" / "home").mkdir(parents=True)
    fresh_state.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "fresh" / "home"))
    monkeypatch.setenv("AXI_STATE_DIR", str(fresh_state))
    monkeypatch.setenv("AXIOM_STATE_DIR", str(fresh_state))
    monkeypatch.setenv("AXIOM_FILE_SECRETS_PATH", str(tmp_path / "fresh" / "secrets.json"))
    copied = tmp_path / "fresh" / "vault-backups"
    D.copy_tree(h.root, copied)
    # no recipient record, no head record, no ledger knowledge beyond the copied directory
    res = h.restore_latest(to=copied)
    assert res.rc == 0 and "RESTORED" in res.out, res.out
    from axiom.infra.vault_backup import sources as S

    assert S.current_fingerprints(fresh_state) == h.gen_digests[3]


@pytest.mark.parametrize("kind", ["state", "vault"])
def test_h_a_backup_from_a_newer_format_is_passed_over_with_the_version_as_the_reason(make, kind):
    h = make(kind)
    h.make_generations(3)
    h.damage(h.newest(), "wrong-version")
    h.break_target()
    before = h.digest()
    res = h.restore_latest()
    j = record("H", h, "newer-format", judge(h, res, before))
    assert j.cls == "restored-from-fallback" and j.matched_gen == 2
    assert "newer" in res.out.lower() or "format 2" in res.out.lower() or "version" in res.out.lower()


@pytest.mark.parametrize("kind", ["state", "vault"])
def test_h_when_the_only_backup_is_from_a_newer_format_it_is_refused_naming_the_version(make, kind):
    h = make(kind)
    h.make_generations(1)
    h.damage(h.newest(), "wrong-version")
    h.break_target()
    before = h.digest()
    res = h.restore_latest()
    j = record("H", h, "newer-format-only", judge(h, res, before))
    assert j.cls == "refused-safely" and ("newer" in res.out.lower() or "format" in res.out.lower())


# ---------------------------------------------------------------------------------------------
# M. clock skew: `latest` is the most recently TAKEN, whatever the timestamps say
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["future-dated-middle", "all-equal"])
@pytest.mark.parametrize("kind", ["state", "vault"])
def test_m_latest_follows_the_order_taken_not_the_clock(make, monkeypatch, kind, mode):
    real = bc.utcnow
    h = make(kind)
    clocks = {1: real(), 2: real() + timedelta(days=3), 3: real()} if mode == "future-dated-middle" else {
        g: real() for g in (1, 2, 3)}
    if mode == "all-equal":
        frozen = real()
        clocks = {g: frozen for g in (1, 2, 3)}
    # generations are created one at a time under the chosen clock
    for g in (1, 2, 3):
        monkeypatch.setattr(bc, "utcnow", lambda g=g: clocks[g])
        h.make_generations(1) if g == 1 else _more_generation(h, kind, g)
    monkeypatch.setattr(bc, "utcnow", real)
    h.break_target()
    before = h.digest()
    j = record("M", h, f"clock-{mode}", judge(h, h.restore_latest(), before))
    assert j.matched_gen == 3 and j.cls == "restored"


def _more_generation(h, kind: str, g: int) -> None:
    if kind == "state":
        h._write_gen(g)
        r = ss.create_snapshot(h.state, h.root, policy=h.policy, force=True, prune=False)
        h.ids.append(r.snapshot_id)
        h.gen_digests[g] = h.digest()
    else:
        h._set_gen(g)
        res = h.vb.create_backup(h.state, force=True)
        assert res.status == "created"
        h.ids.append(res.id)
        h.gen_digests[g] = h.digest()


@pytest.mark.parametrize("kind", ["state", "vault"])
def test_m_a_future_dated_newest_backup_does_not_stop_the_next_scheduled_one(make, monkeypatch, kind):
    real = bc.utcnow
    h = make(kind)
    monkeypatch.setattr(bc, "utcnow", lambda: real() + timedelta(days=30))
    h.make_generations(1)
    monkeypatch.setattr(bc, "utcnow", real)
    if kind == "vault":
        from axiom.infra import vault_backup as vb

        h._set_gen(2)
        out = vb.tick(h.state)
        assert out.ok and out.status == "created", out.message
    else:
        import logging

        from axiom.extensions.builtins.state import skills as state_skills
        from axiom.infra.skills import SkillContext

        ctx = SkillContext(registry=state_skills.bind_default(), state_dir=h.state, logger=logging.getLogger("t"))
        h._write_gen(2)
        r = ctx.registry.invoke("state.snapshot_tick", {}, ctx)
        assert r.ok and r.value.get("snapshot_id") and "interval is" not in " ".join(r.actions_taken)


# ---------------------------------------------------------------------------------------------
# N. retention asked to prune everything must still leave a verified backup
# ---------------------------------------------------------------------------------------------

NOTHING = bc.RetentionPolicy(keep_last=0, keep_recent_hours=0, keep_daily=0, keep_weekly=0, keep_monthly=0,
                             protect_newest=0, max_count=1, max_bytes=1)


@pytest.mark.parametrize("kind", KINDS)
def test_n_retention_that_asks_for_nothing_leaves_a_verified_backup_that_restores(make, kind):
    h = make(kind)
    h.make_generations(3)
    if kind == "state":
        pol = ss.SnapshotPolicy(retention=NOTHING)
        ss.prune_store(h.store, pol, apply=True, now=bc.utcnow() + timedelta(days=400))
    elif kind == "vault":
        from axiom.infra import vault_backup as vb
        from axiom.infra.vault_backup.policy import BackupPolicy

        vb.prune(h.state, h.root, apply=True, policy=BackupPolicy(retention=NOTHING))
    else:
        from axiom.infra import backup as pg

        pg.prune_backups(h.root, NOTHING, apply=True, now=bc.utcnow() + timedelta(days=400))
    h.break_target()
    before = h.digest()
    j = record("N", h, "retention-prunes-everything", judge(h, h.restore_latest(), before))
    assert j.cls in ("restored", "restored-from-fallback") and j.matched_gen is not None


@pytest.mark.parametrize("kind", ["state", "vault", "pg"])
def test_n_when_the_newest_are_corrupt_pruning_keeps_the_last_good_one(make, kind):
    """The disaster: the backups that survive retention are exactly the damaged ones."""
    h = make(kind)
    h.make_generations(3)
    h.damage(h.ids[2], "bitflip")
    h.damage(h.ids[1], "bitflip")
    one = bc.RetentionPolicy(keep_last=1, keep_recent_hours=0, keep_daily=0, keep_weekly=0, keep_monthly=0,
                             protect_newest=1)
    far = bc.utcnow() + timedelta(days=400)
    if kind == "state":
        ss.prune_store(h.store, ss.SnapshotPolicy(retention=one), apply=True, now=far)
    elif kind == "vault":
        from axiom.infra import vault_backup as vb
        from axiom.infra.vault_backup.policy import BackupPolicy

        vb.prune(h.state, h.root, apply=True, policy=BackupPolicy(retention=one))
    else:
        from axiom.infra import backup as pg

        pg.prune_backups(h.root, one, apply=True, now=far)
    h.break_target()
    before = h.digest()
    j = record("N", h, "prune-keeps-last-good", judge(h, h.restore_latest(), before))
    assert j.matched_gen == 1 and j.cls == "restored-from-fallback"
    assert os.path.exists(h.root)
