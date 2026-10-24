# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Backups written by the code BEFORE ``backup_core`` existed must still verify and restore.

The fixtures under ``fixtures/`` are committed bytes produced by commit 9af8fa4e (see
``fixtures/make_fixtures.py``).  If a refactor changes an on-disk format, these fail.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
from axiom.infra import backup_core as bc
from axiom.infra import state_snapshots as ss
from axiom.infra import vault_backup as vb
from axiom.infra.vault_backup import crypto as C

FX = Path(__file__).parent / "fixtures"


@pytest.fixture
def old_snapshots(tmp_path) -> tuple[Path, dict]:
    root = tmp_path / "old-snapshots"
    shutil.copytree(FX / "snapshots_v1", root)
    return root, json.loads((FX / "snapshots_v1.expected.json").read_text())


@pytest.fixture
def old_vault(tmp_path, fast_kdf) -> tuple[Path, dict, str]:
    root = tmp_path / "old-vault"
    shutil.copytree(FX / "vault_v1", root)
    return root, json.loads((FX / "vault_v1.expected.json").read_text()), (FX / "vault_v1.recovery-code.txt").read_text().strip()


# -- state snapshots -------------------------------------------------------------


def test_old_snapshot_store_verifies_deeply(old_snapshots):
    root, exp = old_snapshots
    store = ss.Store(root)
    v = ss.verify_store(store, deep=True)
    assert v.ok, [r.problems for r in v.results] + v.store_problems + v.ledger.problems
    assert sorted(store.ids()) == sorted(exp["ids"])
    assert v.ledger.ok and v.ledger.entries == 5  # snapshot, snapshot, pin, pre-restore snapshot, restore


def test_old_ledger_chain_is_exactly_the_shared_format(old_snapshots):
    root, _ = old_snapshots
    lines = [json.loads(x) for x in (root / "ledger.jsonl").read_text().splitlines()]
    assert all(set(e) == {"seq", "ts", "event", "id", "detail", "prev", "hash"} for e in lines)
    assert bc.HashChainLedger(root / "ledger.jsonl").verify().ok


def test_old_snapshots_restore_byte_for_byte(old_snapshots, tmp_path):
    root, exp = old_snapshots
    for sid, files in exp["files_at"].items():
        state = tmp_path / f"state-{sid}"
        state.mkdir()
        res = ss.restore_snapshot(state, root, sid, apply=True, force=True, _services=lambda _s: [])
        assert res.restored, res.errors
        for rel, text in files.items():
            assert (state / rel).read_text() == text


def test_old_snapshot_latest_is_the_newest_regular_snapshot_not_the_restore_safety_one(old_snapshots, tmp_path):
    """The store's newest snapshot is a pre-restore safety snapshot (made by the generator's restore).
    `latest` skips those: they hold the state a restore found, which is the thing being repaired."""
    root, exp = old_snapshots
    state = tmp_path / "state-latest"
    state.mkdir()
    res = ss.restore_snapshot(state, root, "latest", apply=True, force=True, _services=lambda _s: [])
    assert res.snapshot_id == exp["ids"][1]
    named = ss.restore_snapshot(state, root, exp["ids"][-1], apply=True, force=True, _services=lambda _s: [])
    assert named.snapshot_id == exp["ids"][-1]  # still restorable by name (that is how you undo)


def test_a_new_snapshot_chains_onto_an_old_store(old_snapshots, tmp_path):
    root, exp = old_snapshots
    state = tmp_path / "state-new"
    (state / "identity").mkdir(parents=True)
    (state / "settings.toml").write_text("[new]\nx = 1\n")
    r = ss.create_snapshot(state, root, reason="after refactor", policy=ss.SnapshotPolicy(), force=True)
    assert r.ok
    assert ss.verify_store(ss.open_store(state, root), deep=True).ok
    assert sorted(ss.Store(root).ids()) == sorted([*exp["ids"], r.snapshot_id])


def test_old_manifest_format_is_unchanged(old_snapshots):
    root, exp = old_snapshots
    m = json.loads((root / "snaps" / exp["ids"][0] / "manifest.json").read_text())
    assert set(m) == {"body", "prev", "body_sha256", "chain_hash"}
    assert bc.check_manifest(m) == []


# -- vault backups -----------------------------------------------------------------


def test_old_vault_store_verifies_without_a_key(old_vault, state_dir):
    root, exp, _code = old_vault
    rep = vb.verify(state_dir, root)
    assert rep.ok, rep.problems
    assert rep.checked == 3
    assert {r["id"] for r in vb.list_backups(root)} == set(exp["ids"])


def test_old_vault_archives_verify_deeply_with_the_recorded_unlock(old_vault, state_dir):
    root, exp, code = old_vault
    for bid in exp["values_at"]:
        rep = vb.verify(state_dir, root, ref=bid, unlock=code)
        assert rep.ok, rep.problems
    rep = vb.verify(state_dir, root, ref=exp["passphrase_id"], unlock=exp["passphrase"])
    assert rep.ok, rep.problems


def test_old_vault_archives_restore_their_values(old_vault, state_dir):
    root, exp, code = old_vault
    for bid, values in exp["values_at"].items():
        target = state_dir.parent / f"state-{bid}"
        target.mkdir()
        rep = vb.restore(target, root, bid, unlock=code, apply=True, skip_pre_backup=True)
        assert sorted(rep.written) == sorted(values) and not rep.failed_verification
        s = ForeignCredentialStore(target)
        for name, value in values.items():
            with s.get(name) as sec:
                assert sec.value == value.encode()


def test_old_vault_header_and_ledger_formats_are_unchanged(old_vault):
    root, exp, _code = old_vault
    lines = [json.loads(x) for x in (root / "ledger.jsonl").read_text().splitlines()]
    assert all(set(e) == {"seq", "ts", "event", "id", "detail", "prev", "hash"} for e in lines)
    data = (root / "archives" / f"{exp['ids'][0]}.axvb").read_bytes()
    assert data.startswith(C.MAGIC)
    header, _, _ = C.read_header(data)
    assert header["format"] == 1 and header["enc"]["mode"] == "recipient"


def test_a_new_backup_chains_onto_an_old_vault_store(old_vault, state_dir):
    root, exp, _code = old_vault
    shutil.copy(FX / "vault_v1.recipient.json", (state_dir / "secrets").mkdir() or state_dir / "secrets" / "vault-backup-recipient.json")
    ForeignCredentialStore(state_dir).set("fixture-new", b"FIXTURE-NEW-VALUE-9999", provider="guided")
    res = vb.create_backup(state_dir, root=root, force=True)
    assert res.status == "created"
    assert vb.verify(state_dir, root).ok
    assert len(vb.list_backups(root)) == 4


def test_vault_head_record_is_the_same_file_format(old_vault, state_dir):
    root, _exp, _code = old_vault
    from axiom.infra.vault_backup.store import BackupStore, record_head

    record_head(state_dir, BackupStore(root))
    data = json.loads((state_dir / "secrets" / "vault-backup-head.json").read_text())
    (rec,) = data["roots"].values()
    assert set(rec) == {"seq", "head"} and rec["seq"] == 3
