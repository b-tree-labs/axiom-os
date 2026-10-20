# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Backup, verify, restore, prune, scrub: the behaviours that matter, on real components."""

from __future__ import annotations

import json
import os
import stat

import pytest

from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
from axiom.infra import vault_backup as vb
from axiom.infra.vault_backup import crypto as C

from .conftest import CANARY_A, CANARY_B, CANARY_FILE, PASSPHRASE, damage


def _values(state_dir):
    s = ForeignCredentialStore(state_dir)
    out = {}
    for row in s.list():
        with s.get(row["name"]) as sec:
            out[row["name"]] = sec.value
    return out


# -- creating ----------------------------------------------------------------


def test_backup_without_a_key_refuses_and_writes_nothing(state_dir, vault, root, fast_kdf):
    with pytest.raises(vb.NotConfigured) as e:
        vb.create_backup(state_dir)
    assert "Nothing was written" in str(e.value)
    assert not root.exists()


def test_backup_round_trip_preserves_every_value(state_dir, vault, root, keys):
    r = vb.create_backup(state_dir)
    assert r.status == "created" and r.count == 2
    _, body = C.open_archive(open(r.path, "rb").read(), unlock=keys.code)
    assert {n: e["value"] for n, e in body["entries"].items()} == {
        "alpha-token": CANARY_A.encode(), "bravo-key": CANARY_B.encode()}


def test_file_and_directory_modes_are_private(state_dir, vault, root, keys):
    r = vb.create_backup(state_dir)
    assert stat.S_IMODE(os.stat(r.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(root / "archives").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(root / "ledger.jsonl").st_mode) == 0o600
    from axiom.infra.vault_backup.policy import recipient_path

    assert stat.S_IMODE(os.stat(recipient_path(state_dir)).st_mode) == 0o600


def test_unchanged_vault_is_skipped_and_a_change_is_not(state_dir, vault, root, keys):
    first = vb.create_backup(state_dir)
    again = vb.create_backup(state_dir)
    assert again.status == "unchanged" and again.id == first.id
    vault.set("alpha-token", b"rotated-value-123")
    third = vb.create_backup(state_dir)
    assert third.status == "created" and third.id != first.id
    assert vb.create_backup(state_dir, force=True).status == "created"


def test_metadata_only_churn_does_not_make_a_new_backup(state_dir, vault, root, keys):
    vb.create_backup(state_dir)
    vault.update_metadata("alpha-token", last_verified_at="2026-10-08T00:00:00+00:00")
    assert vb.create_backup(state_dir).status == "unchanged"


def test_key_files_are_in_scope(state_dir, home_dir, vault, root, keys):
    (state_dir / "identity").mkdir()
    (state_dir / "identity" / "private.pem").write_text(CANARY_FILE)
    hm = home_dir / ".config" / "axiom"
    hm.mkdir(parents=True)
    (hm / "audit-hmac-key.env").write_text("AXIOM_AUDIT_HMAC_KEY=" + CANARY_FILE + "-h")
    r = vb.create_backup(state_dir)
    assert set(r.names) == {"alpha-token", "bravo-key", "file:identity/private.pem",
                            "file:~/.config/axiom/audit-hmac-key.env"}


def test_remote_custody_is_metadata_only(state_dir, root, keys):
    """A secret manager the harness only references is out of scope: no value is read."""

    class Remote:
        capabilities = None

        def get(self, ref):  # pragma: no cover
            raise AssertionError("a remote custody value must never be read")

        def put(self, ref, value):
            pass

    s = ForeignCredentialStore(state_dir, value_store=Remote())
    s.set("remote-cred", b"x" * 8, issuer_url="https://issuer.example")
    (state_dir / "identity").mkdir()
    (state_dir / "identity" / "private.pem").write_text(CANARY_FILE)
    r = vb.create_backup(state_dir, store=s)
    assert r.metadata_only == ["remote-cred"] and r.names == ["file:identity/private.pem"]
    shown = vb.show(root, r.id)
    row = next(e for e in shown["entries"] if e["name"] == "remote-cred")
    assert row["scope"] == "metadata-only" and "re-issue at https://issuer.example" in row["recovery_hint"]


def test_passphrase_mode_for_a_one_off_backup(state_dir, vault, root, fast_kdf):
    r = vb.create_backup(state_dir, passphrase=PASSPHRASE)
    vb.restore(state_dir, root, r.id, unlock=PASSPHRASE)
    with pytest.raises(C.DecryptionError):
        vb.restore(state_dir, root, r.id, unlock="not the passphrase at all")


def test_verify_on_write_removes_an_archive_that_does_not_decrypt(state_dir, vault, root, keys, monkeypatch):
    real = C.open_with_content_key

    def bad(data, key):
        body = real(data, key)
        next(iter(body["entries"].values()))["value"] = b"different"
        return body

    monkeypatch.setattr(C, "open_with_content_key", bad)
    with pytest.raises(C.VaultBackupError, match="verify-on-write"):
        vb.create_backup(state_dir)
    assert vb.list_backups(root) == [] and not list((root / "archives").glob("*.axvb"))


# -- listing / showing -----------------------------------------------------------


def test_show_needs_no_key_and_lists_names_and_fingerprints(state_dir, vault, root, keys):
    r = vb.create_backup(state_dir)
    s = vb.show(root, r.id)
    assert [e["name"] for e in s["entries"]] == ["alpha-token", "bravo-key"]
    assert all(len(e["fingerprint"]) >= 8 for e in s["entries"])
    assert s["unlock"] == "recipient"


def test_latest_resolves_to_the_newest_verified_backup(state_dir, vault, root, keys):
    a = vb.create_backup(state_dir)
    vault.set("alpha-token", b"v2-value-xxxx")
    b = vb.create_backup(state_dir)
    assert vb.engine.resolve_id(root, "latest") == b.id
    assert vb.engine.resolve_id(root, a.id[:12] + a.id[12:]) == a.id
    damage(root / "archives" / f"{b.id}.axvb", "flip")
    assert vb.engine.resolve_id(root, "latest") == a.id


# -- restore -------------------------------------------------------------------------


def test_restore_dry_run_changes_nothing_and_lists_names(state_dir, vault, root, keys):
    r = vb.create_backup(state_dir)
    vault.remove("alpha-token")
    vault.set("bravo-key", b"changed-value-9")
    rep = vb.restore(state_dir, root, r.id, unlock=keys.code)
    assert rep.applied is False
    assert {i.name: i.action for i in rep.items} == {"alpha-token": "add", "bravo-key": "conflict"}
    assert not vault.exists("alpha-token")


def test_restore_never_overwrites_a_conflict_silently(state_dir, vault, root, keys):
    r = vb.create_backup(state_dir)
    vault.set("bravo-key", b"changed-value-9")
    rep = vb.restore(state_dir, root, r.id, unlock=keys.code, apply=True)
    assert rep.skipped_conflicts == ["bravo-key"] and rep.written == []
    assert _values(state_dir)["bravo-key"] == b"changed-value-9"


def test_restore_with_overwrite_takes_a_pre_restore_backup_first(state_dir, vault, root, keys):
    r = vb.create_backup(state_dir)
    vault.set("bravo-key", b"changed-value-9")
    rep = vb.restore(state_dir, root, r.id, unlock=keys.code, apply=True, overwrite=True)
    assert rep.written == ["bravo-key"] and rep.failed_verification == []
    assert _values(state_dir)["bravo-key"] == CANARY_B.encode()
    assert rep.pre_restore_id
    # ...and the pre-restore backup holds what was overwritten.
    _, body = C.open_archive((root / "archives" / f"{rep.pre_restore_id}.axvb").read_bytes(), unlock=keys.code)
    assert body["entries"]["bravo-key"]["value"] == b"changed-value-9"


def test_restore_only_filters_by_name(state_dir, vault, root, keys):
    r = vb.create_backup(state_dir)
    vault.remove("alpha-token")
    vault.remove("bravo-key")
    rep = vb.restore(state_dir, root, r.id, unlock=keys.code, apply=True, only=["alpha-*"])
    assert rep.written == ["alpha-token"] and not vault.exists("bravo-key")


def test_restore_goes_through_the_store_api_and_keeps_the_index_consistent(state_dir, vault, root, keys):
    r = vb.create_backup(state_dir)
    before = vault.metadata("alpha-token")
    vault.remove("alpha-token")
    vb.restore(state_dir, root, r.id, unlock=keys.code, apply=True)
    after = vault.metadata("alpha-token")
    assert after["fingerprint"] == before["fingerprint"] and after["notes"] == "first"
    assert after["provider"] == "guided"


def test_restore_into_an_empty_vault_needs_no_key_configured(state_dir, vault, root, keys):
    """The disaster case: new machine, no recipient file, only the archive and the code."""
    r = vb.create_backup(state_dir)
    from axiom.infra.vault_backup.policy import recipient_path

    recipient_path(state_dir).unlink()
    for n in ("alpha-token", "bravo-key"):
        vault.remove(n)
    rep = vb.restore(state_dir, root, r.id, unlock=keys.code, apply=True)
    assert sorted(rep.written) == ["alpha-token", "bravo-key"] and rep.pre_restore_id is None


def test_restore_over_a_nonempty_vault_without_a_key_refuses_before_changing_anything(state_dir, vault, root, keys):
    r = vb.create_backup(state_dir)
    from axiom.infra.vault_backup.policy import recipient_path

    recipient_path(state_dir).unlink()
    vault.set("bravo-key", b"changed-value-9")
    with pytest.raises(vb.NotConfigured):
        vb.restore(state_dir, root, r.id, unlock=keys.code, apply=True, overwrite=True)
    assert _values(state_dir)["bravo-key"] == b"changed-value-9"


def test_restore_files_back_to_their_paths_atomically(state_dir, vault, root, keys):
    (state_dir / "identity").mkdir()
    p = state_dir / "identity" / "private.pem"
    p.write_text(CANARY_FILE)
    r = vb.create_backup(state_dir)
    p.unlink()
    rep = vb.restore(state_dir, root, r.id, unlock=keys.code, apply=True, only=["file:*"])
    assert rep.written == ["file:identity/private.pem"] and p.read_text() == CANARY_FILE
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


# -- wrong key, damage, versions ----------------------------------------------------


def test_wrong_code_is_generic_and_changes_nothing(state_dir, vault, root, keys):
    r = vb.create_backup(state_dir)
    vault.remove("alpha-token")
    with pytest.raises(C.DecryptionError) as e:
        vb.restore(state_dir, root, r.id, unlock=C.generate_recovery_code(), apply=True)
    assert str(e.value) == C.GENERIC_FAILURE and not vault.exists("alpha-token")


def test_mistyped_code_fails_the_same_way_as_a_wrong_one(state_dir, vault, root, keys):
    r = vb.create_backup(state_dir)
    with pytest.raises(C.DecryptionError) as e:
        vb.restore(state_dir, root, r.id, unlock="AAAA-BBBB")
    assert str(e.value) == C.GENERIC_FAILURE


@pytest.mark.parametrize("how", ["truncate", "flip", "zero", "garbage"])
def test_damaged_archives_are_detected_without_a_key_and_never_restored(state_dir, vault, root, keys, how):
    r = vb.create_backup(state_dir)
    damage(root / "archives" / f"{r.id}.axvb", how)
    rep = vb.verify(state_dir, root)
    assert not rep.ok and r.id in rep.problems[0]
    row = vb.list_backups(root)[0]
    assert row["verified"] is False
    with pytest.raises(vb.engine.NoSuchBackup):
        vb.engine.resolve_id(root, "latest")
    vault.remove("alpha-token")
    with pytest.raises(C.VaultBackupError):
        vb.restore(state_dir, root, r.id, unlock=keys.code, apply=True)
    assert not vault.exists("alpha-token")


def test_deleted_archive_is_reported_missing(state_dir, vault, root, keys):
    r = vb.create_backup(state_dir)
    damage(root / "archives" / f"{r.id}.axvb", "delete")
    rep = vb.verify(state_dir, root)
    assert not rep.ok and "missing" in rep.problems[0]


def test_an_unrecorded_archive_in_the_directory_is_flagged(state_dir, vault, root, keys):
    r = vb.create_backup(state_dir)
    (root / "archives" / "vb-planted.axvb").write_bytes((root / "archives" / f"{r.id}.axvb").read_bytes())
    rep = vb.verify(state_dir, root)
    assert not rep.ok and rep.orphans == ["vb-planted"]


def test_a_newer_format_is_refused_cleanly(state_dir, vault, root, keys):
    import struct

    r = vb.create_backup(state_dir)
    path = root / "archives" / f"{r.id}.axvb"
    data = path.read_bytes()
    h, hb, off = C.read_header(data)
    h2 = json.loads(hb)
    h2["format"] = 99
    nh = C._canon(h2)
    path.write_bytes(C.MAGIC + struct.pack(">I", len(nh)) + nh + data[off:])
    with pytest.raises(C.FormatError):  # sha mismatch is reported first via the ledger
        vb.restore(state_dir, root, r.id, unlock=keys.code)
    with pytest.raises(C.UnsupportedVersion, match="newer"):
        C.open_archive(path.read_bytes(), unlock=keys.code)


def test_deep_verify_decrypts_and_checks_fingerprints(state_dir, vault, root, keys):
    vb.create_backup(state_dir)
    rep = vb.verify(state_dir, root, unlock=keys.code)
    assert rep.ok and rep.deep
    bad = vb.verify(state_dir, root, unlock=C.generate_recovery_code())
    assert not bad.ok


# -- ledger: tamper and rollback evidence ---------------------------------------------------


def test_ledger_tamper_is_detected(state_dir, vault, root, keys):
    vb.create_backup(state_dir)
    vault.set("alpha-token", b"v2-value-xxxx")
    vb.create_backup(state_dir)
    lines = (root / "ledger.jsonl").read_text().splitlines()
    e = json.loads(lines[0])
    e["detail"]["count"] = 99
    lines[0] = json.dumps(e)
    (root / "ledger.jsonl").write_text("\n".join(lines) + "\n")
    rep = vb.verify(state_dir, root)
    assert not rep.ok and any("ledger" in p for p in rep.problems)


def test_rolling_the_backup_directory_back_is_detected(state_dir, vault, root, keys):
    vb.create_backup(state_dir)
    old_ledger = (root / "ledger.jsonl").read_bytes()
    vault.set("alpha-token", b"v2-value-xxxx")
    vb.create_backup(state_dir)
    (root / "ledger.jsonl").write_bytes(old_ledger)
    rep = vb.verify(state_dir, root)
    assert any("ROLLBACK" in p for p in rep.problems)


# -- second copy, other locations --------------------------------------------------------------


def test_backup_to_another_directory_and_a_configured_second_copy(state_dir, vault, tmp_path, keys):
    other = tmp_path / "external-disk" / "vault"
    r = vb.create_backup(state_dir, root=other)
    assert (other / "archives" / f"{r.id}.axvb").exists()
    second = tmp_path / "second"
    (state_dir / "settings.toml").write_text(f'[vault.backup]\nsecond_copy = "{second}"\n')
    r2 = vb.create_backup(state_dir, force=True)
    assert (second / "archives" / f"{r2.id}.axvb").read_bytes() == open(r2.path, "rb").read()
    assert vb.verify(state_dir, second).ok


def test_a_failing_second_copy_warns_and_leaves_the_primary_intact(state_dir, vault, tmp_path, keys):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    (state_dir / "settings.toml").write_text(f'[vault.backup]\nsecond_copy = "{blocker}/sub"\n')
    r = vb.create_backup(state_dir)
    assert r.status == "created" and any("second copy" in w for w in r.warnings)


def test_a_missing_or_renamed_backup_directory_is_reported_not_crashed(state_dir, vault, root, keys, tmp_path):
    vb.create_backup(state_dir)
    root.rename(tmp_path / "renamed")
    assert vb.list_backups(root) == []
    r = vb.create_backup(state_dir)  # a fresh store is created; the old one is simply elsewhere
    assert r.status == "created"
    assert vb.verify(state_dir, tmp_path / "renamed").ok


# -- failure during write ---------------------------------------------------------------------------


def test_disk_full_during_write_leaves_the_existing_backups_untouched(state_dir, vault, root, keys, monkeypatch):
    import errno

    first = vb.create_backup(state_dir)
    before = (root / "archives" / f"{first.id}.axvb").read_bytes()
    ledger_before = (root / "ledger.jsonl").read_bytes()
    vault.set("alpha-token", b"v2-value-xxxx")

    def full(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    # A scoped patch: monkeypatch.undo() would also revert the state-dir and secrets-path settings the
    # fixtures made with this same object, and the rest of the test would then read the DEFAULT vault.
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "fsync", full)
        with pytest.raises(vb.DiskPressureError):
            vb.create_backup(state_dir)
    assert (root / "archives" / f"{first.id}.axvb").read_bytes() == before
    assert (root / "ledger.jsonl").read_bytes() == ledger_before
    assert vb.engine.BackupStore(root).stray_temp_files() == []


def test_too_little_free_space_refuses_before_writing(state_dir, vault, root, keys, monkeypatch):
    from axiom.infra import backup_core as bc

    monkeypatch.setattr(bc, "free_bytes", lambda p: (10, 1000))
    with pytest.raises(vb.DiskPressureError, match="nothing was written"):
        vb.create_backup(state_dir)


def test_an_interrupted_write_leaves_only_ignorable_temp_debris(state_dir, vault, root, keys, monkeypatch):
    class Boom(BaseException):
        pass

    def die(*a, **k):
        raise Boom

    with monkeypatch.context() as scoped:  # scoped, so the fixtures' own settings stay in force
        scoped.setattr(os, "replace", die)
        with pytest.raises(Boom):
            vb.create_backup(state_dir)
    assert vb.list_backups(root) == []
    r = vb.create_backup(state_dir)
    assert r.status == "created"


def test_concurrent_backup_runs_serialise_on_the_lock(state_dir, vault, root, keys):
    store = vb.engine.BackupStore(root)
    with store.locked():
        with pytest.raises(vb.BackupBusy):
            with vb.engine.BackupStore(root).locked(timeout=0.2):
                pass


def test_concurrent_backups_from_threads_make_one_archive(state_dir, vault, root, keys):
    import concurrent.futures as cf

    with cf.ThreadPoolExecutor(4) as ex:
        results = list(ex.map(lambda _: vb.create_backup(state_dir), range(4)))
    assert sorted(r.status for r in results).count("created") == 1
    assert len(vb.list_backups(root)) == 1


# -- retention ----------------------------------------------------------------------------------------


def _age(root, bid, hours):
    """Rewrite a backup's recorded time in a ledger-consistent way (test-only)."""
    from axiom.infra import backup_core as bc

    lines = [json.loads(x) for x in (root / "ledger.jsonl").read_text().splitlines()]
    prev = ""
    for e in lines:
        if e["event"] == "backup" and e["id"] == bid:
            e["detail"]["created_at"] = bc.iso(bc.utcnow() - __import__("datetime").timedelta(hours=hours))
        e["prev"] = prev
        e.pop("hash", None)
        e["hash"] = bc.sha256_bytes(bc.canon(e))
        prev = e["hash"]
    (root / "ledger.jsonl").write_text("\n".join(bc.canon(e).decode() for e in lines) + "\n")


def test_prune_dry_run_then_apply_never_deletes_the_newest_verified(state_dir, vault, root, keys):
    (state_dir / "settings.toml").write_text(
        "[vault.backup]\nkeep_last = 2\nkeep_daily = 1\nkeep_weekly = 1\nkeep_monthly = 1\n")
    ids = []
    for i in range(8):
        vault.set("alpha-token", f"value-number-{i}".encode())
        ids.append(vb.create_backup(state_dir).id)
    for n, bid in enumerate(ids):
        _age(root, bid, 24 * 40 + n / 10)  # all old, same day: outside the recent window
    plan = vb.prune(state_dir, root)
    assert plan.applied is False and plan.plan.delete
    assert len(vb.list_backups(root)) == 8
    done = vb.prune(state_dir, root, apply=True)
    remaining = [r["id"] for r in vb.list_backups(root)]
    assert done.deleted and len(remaining) < 8
    assert vb.engine.resolve_id(root, "latest") in remaining


def test_prune_respects_pins_and_never_deletes_the_last_backup(state_dir, vault, root, keys):
    vault.set("alpha-token", b"only-one-value")
    only = vb.create_backup(state_dir)
    _age(root, only.id, 24 * 400)
    out = vb.prune(state_dir, root, apply=True)
    assert out.deleted == [] and len(vb.list_backups(root)) == 1

    (state_dir / "settings.toml").write_text(
        "[vault.backup]\nkeep_last = 1\nkeep_daily = 1\nkeep_weekly = 1\nkeep_monthly = 1\nprotect_newest = 1\n")
    ids = []
    for i in range(6):
        vault.set("alpha-token", f"value-number-{i}".encode())
        ids.append(vb.create_backup(state_dir).id)
    for n, bid in enumerate(ids):
        _age(root, bid, 24 * 60 + n / 10)
    pinned = ids[-1]  # the OLDEST: nothing else protects it
    vb.engine.set_pinned(root, pinned, True, state_dir)
    vb.prune(state_dir, root, apply=True)
    kept = [r["id"] for r in vb.list_backups(root)]
    assert pinned in kept and len(kept) < 6


def test_vault_retention_defaults_differ_from_snapshots_as_documented():
    from axiom.infra.state_snapshots import RetentionPolicy
    from axiom.infra.vault_backup.policy import VAULT_RETENTION as V

    s = RetentionPolicy()
    assert V.keep_recent_hours < s.keep_recent_hours and V.keep_monthly > s.keep_monthly
    assert V.protect_newest >= 3


# -- exposure interplay ------------------------------------------------------------------------------------


def test_taint_excludes_from_latest_and_withholds_on_restore(state_dir, vault, root, keys):
    a = vb.create_backup(state_dir)
    fp = vault.metadata("alpha-token")["fingerprint"]
    ids = vb.engine.taint(state_dir, root, "alpha-token", fp, where="transcript")
    assert ids == [a.id]
    with pytest.raises(vb.engine.NoSuchBackup, match="exposed"):
        vb.engine.resolve_id(root, "latest")
    vault.remove("alpha-token")
    vault.remove("bravo-key")
    rep = vb.restore(state_dir, root, a.id, unlock=keys.code, apply=True)
    assert rep.withheld == ["alpha-token"] and rep.written == ["bravo-key"]
    assert not vault.exists("alpha-token")


def test_restoring_an_exposed_value_on_request_warns_loudly(state_dir, vault, root, keys):
    a = vb.create_backup(state_dir)
    vb.engine.taint(state_dir, root, "alpha-token", vault.metadata("alpha-token")["fingerprint"])
    vault.remove("alpha-token")
    rep = vb.restore(state_dir, root, a.id, unlock=keys.code, apply=True, include_tainted=True, only=["alpha-token"])
    assert rep.written == ["alpha-token"] and any("EXPOSED" in w for w in rep.warnings)


def test_a_backup_after_rotation_is_not_tainted(state_dir, vault, root, keys):
    a = vb.create_backup(state_dir)
    vb.engine.taint(state_dir, root, "alpha-token", vault.metadata("alpha-token")["fingerprint"])
    vault.set("alpha-token", b"rotated-after-exposure")
    b = vb.create_backup(state_dir)
    assert vb.engine.resolve_id(root, "latest") == b.id
    rows = {r["id"]: r for r in vb.list_backups(root)}
    assert rows[a.id]["tainted"] == ["alpha-token"] and rows[b.id]["tainted"] == []


def test_scrub_reencrypts_affected_backups_without_the_exposed_value(state_dir, vault, root, keys):
    a = vb.create_backup(state_dir)
    vb.engine.taint(state_dir, root, "alpha-token", vault.metadata("alpha-token")["fingerprint"])
    out = vb.scrub(state_dir, root, "alpha-token", unlock=keys.code)
    assert out.rewritten == [a.id] and out.purged
    raw = (root / "archives" / f"{a.id}.axvb").read_bytes()
    header, body = C.open_archive(raw, unlock=keys.code)
    assert list(body["entries"]) == ["bravo-key"]
    assert [m["name"] for m in header["entries"]] == ["bravo-key"]
    assert vb.verify(state_dir, root, unlock=keys.code).ok
    assert vb.engine.resolve_id(root, "latest") == a.id  # no longer tainted: it no longer holds the value


def test_scrub_with_the_wrong_code_changes_nothing(state_dir, vault, root, keys):
    a = vb.create_backup(state_dir)
    vb.engine.taint(state_dir, root, "alpha-token", vault.metadata("alpha-token")["fingerprint"])
    before = (root / "archives" / f"{a.id}.axvb").read_bytes()
    out = vb.scrub(state_dir, root, "alpha-token", unlock=C.generate_recovery_code())
    assert out.failed == [a.id] and not out.purged
    assert (root / "archives" / f"{a.id}.axvb").read_bytes() == before


def test_scrub_without_a_key_only_marks(state_dir, vault, root, keys):
    a = vb.create_backup(state_dir)
    vb.engine.taint(state_dir, root, "alpha-token", vault.metadata("alpha-token")["fingerprint"])
    before = (root / "archives" / f"{a.id}.axvb").read_bytes()
    out = vb.scrub(state_dir, root, "alpha-token")
    assert out.tainted_ids == [a.id] and out.rewritten == []
    assert (root / "archives" / f"{a.id}.axvb").read_bytes() == before


def test_scrub_all_versions_removes_every_stored_version(state_dir, vault, root, keys):
    a = vb.create_backup(state_dir)
    vault.set("alpha-token", b"second-version-xx")
    b = vb.create_backup(state_dir)
    out = vb.scrub(state_dir, root, "alpha-token", unlock=keys.code, all_versions=True)
    assert sorted(out.rewritten) == sorted([a.id, b.id])


def test_taint_exposed_reads_only_the_fingerprint_from_the_index(state_dir, vault, root, keys, monkeypatch):
    vb.create_backup(state_dir)
    monkeypatch.setattr(ForeignCredentialStore, "get",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("value read")))
    ids = vb.taint_exposed(state_dir, "alpha-token", where="log")
    assert len(ids) == 1


# -- tick and the change hook -----------------------------------------------------------------------------------


def test_scheduled_run_without_a_key_says_so_and_writes_nothing(state_dir, vault, root, fast_kdf):
    t = vb.tick(state_dir)
    assert t.ok is False and t.status == "not-configured"
    assert "NOTHING was written" in t.message and not root.exists()


def test_scheduled_run_with_a_key_needs_no_prompt_and_respects_the_interval(state_dir, vault, root, keys):
    first = vb.tick(state_dir)
    assert first.ok and first.status == "created"
    assert vb.tick(state_dir).status == "not-due"
    assert vb.tick(state_dir, force=True).status == "unchanged"


def test_change_hook_is_debounced_and_picked_up_by_the_tick(state_dir, vault, root, keys):
    assert vb.note_change(state_dir, "secrets set a") == "backed-up"
    vault.set("alpha-token", b"v2-value-xxxx")
    assert vb.note_change(state_dir, "secrets set a") == "deferred"
    assert len(vb.list_backups(root)) == 1
    from axiom.infra.vault_backup.policy import pending_path

    assert pending_path(state_dir).exists()
    (state_dir / "settings.toml").write_text("[vault.backup]\ndebounce_seconds = 0\n")
    t = vb.tick(state_dir)
    assert t.status == "created" and not pending_path(state_dir).exists()


def test_change_hook_is_silent_when_not_configured_and_never_raises(state_dir, vault, fast_kdf, monkeypatch):
    assert vb.note_change(state_dir, "x") == "not-configured"
    vb.init_key(state_dir)
    monkeypatch.setattr(vb.engine, "create_backup", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert vb.note_change(state_dir, "x") == "error"


def test_state_snapshots_do_not_contain_vault_backups(state_dir, vault, root, keys):
    from axiom.infra import state_snapshots as ss

    vb.create_backup(state_dir)
    assert ss.classify("vault-backups/archives/vb-1.axvb").action == "exclude"


def test_a_forged_ledger_entry_with_a_valid_hash_but_the_wrong_link_is_detected(state_dir, vault, root, keys):
    from axiom.infra import backup_core as bc

    vb.create_backup(state_dir)
    vault.set("alpha-token", b"v2-value-xxxx")
    vb.create_backup(state_dir)
    lines = [json.loads(x) for x in (root / "ledger.jsonl").read_text().splitlines()]
    forged = dict(lines[1], prev="0" * 64)
    forged.pop("hash")
    forged["hash"] = bc.sha256_bytes(bc.canon(forged))  # self-consistent, but linked to nothing
    lines[1] = forged
    (root / "ledger.jsonl").write_text("\n".join(bc.canon(e).decode() for e in lines) + "\n")
    rep = vb.verify(state_dir, root)
    assert not rep.ok and any("chain does not link" in p for p in rep.problems)


def test_restore_notices_when_a_write_did_not_take(state_dir, vault, root, keys, monkeypatch):
    from axiom.infra.vault_backup import sources

    r = vb.create_backup(state_dir)
    vault.remove("alpha-token")
    monkeypatch.setattr(sources, "write_entry", lambda *a, **k: None)  # a store that silently drops writes
    rep = vb.restore(state_dir, root, r.id, unlock=keys.code, apply=True)
    assert rep.failed_verification == ["alpha-token"] and any("VERIFICATION FAILED" in w for w in rep.warnings)


def test_a_backend_error_that_echoes_a_value_is_never_repeated(state_dir, vault, root, keys, monkeypatch):
    class Leaky(ForeignCredentialStore):
        def get(self, name):
            raise RuntimeError(f"backend said: {CANARY_A}")

    r = vb.create_backup(state_dir, store=Leaky(state_dir))
    assert r.status == "empty" and r.skipped and CANARY_A not in json.dumps(r.skipped)
    assert "RuntimeError" in r.skipped[0]["reason"]
