# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Vault backup engine: backup, list/show/verify, restore, prune, taint/scrub, tick.

Everything here returns NAMES and FINGERPRINTS.  Values exist only inside
:class:`~.sources.Entry` objects and the decrypted body, and never leave this
module except through ``sources.write_entry`` on restore.  Exceptions carry no
values and no backend error text.
"""

from __future__ import annotations

import json
import logging
import secrets as _secrets
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from axiom.infra import backup_core as bc
from axiom.infra import state_snapshots as ss

from . import crypto as C
from . import sources as S
from .policy import (
    BackupPolicy,
    default_root,
    load_policy,
    pending_path,
    recipient_path,
)
from .store import (
    BackupBusy,
    BackupStore,
    DiskPressureError,
    check_head,
    record_head,
    verify_ledger,
)

log = logging.getLogger(__name__)

Unlock = str | Callable[[], str] | None

__all__ = [
    "DECRYPT_HELP", "NoSuchBackup", "NoUsableBackup", "PreRestoreFailed", "VaultKind",
    "BackupBusy", "DiskPressureError", "NotConfigured", "create_backup", "init_key", "key_info",
    "list_backups", "load_recipient", "note_change", "prune", "restore", "resolve_id", "scrub",
    "show", "taint", "taint_exposed", "tick", "verify",
]


class NotConfigured(C.VaultBackupError):
    """No recipient key: a non-interactive backup cannot encrypt, and will not write plaintext."""


class AlreadyConfigured(C.VaultBackupError):
    pass


class NoSuchBackup(C.VaultBackupError):
    pass


class NoUsableBackup(NoSuchBackup):
    """Backups exist but none can be used; the message lists why each failed and what to try."""


class PreRestoreFailed(C.VaultBackupError):
    """The safety backup taken before a restore could not be made, so nothing was restored."""


def _cli() -> str:
    return ss._brand_cli()


def _unlock_value(unlock: Unlock) -> str:
    v = unlock() if callable(unlock) else unlock
    if not v:
        raise C.DecryptionError()
    return v


# ===========================================================================
# Recipient (the key custody record)
# ===========================================================================


def load_recipient(state_dir: Path) -> C.Recipient | None:
    try:
        return C.Recipient.from_dict(json.loads(recipient_path(state_dir).read_text("utf-8")))
    except (OSError, ValueError, C.FormatError):
        return None


@dataclass
class KeyInit:
    recipient: C.Recipient
    #: Shown once by the caller and never stored.  Excluded from repr so it cannot reach a log.
    recovery_code: str | None = field(default=None, repr=False)


def init_key(state_dir: Path, *, passphrase: str | None = None, force: bool = False,
             kdf: dict[str, int] | None = None) -> KeyInit:
    """Create the recipient key.  With no passphrase, mint a recovery code (returned once)."""
    if load_recipient(state_dir) is not None and not force:
        raise AlreadyConfigured(
            "a backup key already exists; replacing it means older backups still need the OLD "
            "recovery code. Re-run with force to replace it."
        )
    code = None
    if passphrase is None:
        code = C.generate_recovery_code()
        rec = C.create_recipient(code, label="recovery-code", kdf=kdf, created_at=bc.iso(bc.utcnow()))
    else:
        rec = C.create_recipient(passphrase, label="passphrase", kdf=kdf, created_at=bc.iso(bc.utcnow()))
    bc.mkdir_private(recipient_path(state_dir).parent)
    bc.atomic_publish(recipient_path(state_dir), json.dumps(rec.to_dict(), sort_keys=True).encode())
    return KeyInit(recipient=rec, recovery_code=code)


def key_info(state_dir: Path, root: Path | None = None) -> dict[str, Any]:
    rec = load_recipient(state_dir)
    policy = load_policy(state_dir)
    root = Path(root) if root else default_root(state_dir)
    store = BackupStore(root)
    per_key: dict[str, int] = {}
    for bid in store.archive_ids_on_disk():
        try:
            h, _, _ = C.read_header(store.read_archive(bid))
            fp = h["enc"].get("recipient_fp") or h["enc"]["mode"]
        except (OSError, C.VaultBackupError):
            continue
        per_key[fp] = per_key.get(fp, 0) + 1
    return {
        "configured": rec is not None,
        "recipient_fingerprint": rec.fingerprint if rec else None,
        "unlock": rec.label if rec else None,
        "created_at": rec.created_at if rec else None,
        "root": str(root),
        "second_copy": policy.second_copy or None,
        "same_device_as_state": ss.same_device(root if root.exists() else root.parent, Path(state_dir)),
        "archives_by_recipient": per_key,
    }


# ===========================================================================
# Reading the store
# ===========================================================================


@dataclass
class Rec:
    id: str
    created_at: str = ""
    sha256: str = ""
    size: int = 0
    count: int = 0
    set_digest: str = ""
    reason: str = ""
    trigger: str = ""
    host: str = ""
    pinned: bool = False
    deleted: bool = False
    scrubbed: list[str] = field(default_factory=list)
    seq: int = 0


def _fold(store: BackupStore) -> tuple[dict[str, Rec], list[dict[str, Any]]]:
    recs: dict[str, Rec] = {}
    taints: list[dict[str, Any]] = []
    for e in store.ledger_entries():
        if "_corrupt" in e:
            continue
        ev, bid, d = e.get("event"), e.get("id"), e.get("detail") or {}
        if ev == "backup" and bid:
            recs[bid] = Rec(id=bid, created_at=d.get("created_at", e.get("ts", "")), sha256=d.get("sha256", ""),
                            size=d.get("size", 0), count=d.get("count", 0), set_digest=d.get("set_digest", ""),
                            reason=d.get("reason", ""), trigger=d.get("trigger", ""), host=d.get("host", ""),
                            seq=int(e.get("seq") or 0))
        elif ev == "scrub" and bid in recs:
            recs[bid].sha256, recs[bid].size, recs[bid].count = d.get("sha256", ""), d.get("size", 0), d.get("count", 0)
            recs[bid].scrubbed += list(d.get("names", []))
        elif ev == "prune":
            for i in d.get("ids", []):
                if i in recs:
                    recs[i].deleted = True
        elif ev == "pin" and bid in recs:
            recs[bid].pinned = True
        elif ev == "unpin" and bid in recs:
            recs[bid].pinned = False
        elif ev == "taint":
            taints.append({"name": d.get("name"), "fingerprint": d.get("fingerprint"),
                           "where": d.get("where", ""), "ts": e.get("ts", "")})
    return recs, taints


def _taint_keys(taints: Iterable[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(t["name"], t["fingerprint"]) for t in taints if t.get("name") and t.get("fingerprint")}


def _inspect(store: BackupStore, rec: Rec) -> dict[str, Any]:
    """Key-free check of one backup.  ``status`` is ok | missing | corrupt."""
    out: dict[str, Any] = {"status": "ok", "header": None}
    try:
        data = store.read_archive(rec.id)
    except OSError:
        out["status"] = "missing"
        return out
    if bc.sha256_bytes(data) != rec.sha256:
        out["status"] = "corrupt"
        out["detail"] = "file does not match the ledger (damaged or replaced)"
    try:
        header, _, _ = C.read_header(data)
        if header.get("id") != rec.id:
            out["status"] = "corrupt"
            out["detail"] = "file belongs to a different backup id"
        out["header"] = header
    except C.VaultBackupError as exc:
        out["status"] = "corrupt"
        out["detail"] = str(exc)
    return out


def list_backups(root: Path, *, include_deleted: bool = False) -> list[dict[str, Any]]:
    """Most recently TAKEN first (ledger order, so a wrong clock cannot make an old one "newest").
    Names only; never a value."""
    store = BackupStore(root)
    recs, taints = _fold(store)
    tk = _taint_keys(taints)
    rows = []
    for rec in recs.values():
        if rec.deleted and not include_deleted:
            continue
        insp = _inspect(store, rec) if not rec.deleted else {"status": "deleted", "header": None}
        header = insp["header"] or {}
        tainted = sorted(m["name"] for m in header.get("entries", []) if (m.get("name"), m.get("fingerprint")) in tk)
        rows.append({
            "seq": rec.seq, "id": rec.id, "created_at": rec.created_at, "count": rec.count, "size": rec.size,
            "reason": rec.reason, "trigger": rec.trigger, "host": rec.host, "pinned": rec.pinned,
            "status": insp["status"], "verified": insp["status"] == "ok", "tainted": tainted,
            "scrubbed": sorted(set(rec.scrubbed)), "detail": insp.get("detail"),
        })
    rows.sort(key=lambda r: (r["seq"], r["created_at"], r["id"]), reverse=True)
    return rows


def resolve_id(root: Path, ref: str) -> str:
    """``latest`` = newest verified backup with nothing tainted.  Else an id or unique prefix."""
    rows = list_backups(root)
    if not rows:
        why = bc.directory_problem(root, "backup directory")
        unrecorded = BackupStore(root).archive_ids_on_disk() if not why else []
        raise NoSuchBackup(
            (why + ". " if why else
             (f"the ledger records no backups, but {len(unrecorded)} archive file(s) are present in {root}: "
              "the ledger was deleted or replaced, and an archive the ledger does not record is never trusted "
              "or selected. Restore the ledger from a second copy, or open the archives directly with the "
              "emergency script. " if unrecorded else f"no backups in {root}. "))
            + f"If they are elsewhere, point at them (`{_cli()} vault ls --from DIR`) or use a second copy; if the "
              "CLI is unusable, `python3 -I axi-restore.py vault list DIR` reads them."
        )
    if ref == "latest":
        for r in rows:
            if r["verified"] and not r["tainted"] and r["trigger"] != "restore":
                return r["id"]
        raise NoSuchBackup(
            "no verified backup free of exposed credentials. Pick one explicitly with its id "
            f"(`{_cli()} vault ls`), or purge exposed values first (`{_cli()} vault scrub --name <credential>`)."
        )
    hits = [r["id"] for r in rows if r["id"] == ref] or [r["id"] for r in rows if r["id"].startswith(ref)]
    if len(hits) != 1:
        raise NoSuchBackup(f"{'ambiguous' if hits else 'unknown'} backup {ref!r}")
    return hits[0]


def show(root: Path, ref: str) -> dict[str, Any]:
    """The cleartext manifest of one backup.  Needs no key."""
    store = BackupStore(root)
    bid = resolve_id(root, ref)
    recs, taints = _fold(store)
    insp = _inspect(store, recs[bid])
    if insp["header"] is None:
        raise C.FormatError(insp.get("detail") or "backup unreadable")
    h = insp["header"]
    tk = _taint_keys(taints)
    entries = [{**{k: v for k, v in m.items() if k != "value"},
                "tainted": (m.get("name"), m.get("fingerprint")) in tk} for m in h.get("entries", [])]
    return {"id": bid, "format": h["format"], "created_at": h.get("created_at"), "host": h.get("host"),
            "axiom_version": h.get("axiom_version"), "count": h.get("count"), "reason": recs[bid].reason,
            "unlock": h["enc"]["mode"], "recipient_fingerprint": h["enc"].get("recipient_fp"),
            "status": insp["status"], "entries": entries}


# ===========================================================================
# Backup
# ===========================================================================


@dataclass
class BackupResult:
    status: str  # created | unchanged | empty
    id: str | None = None
    count: int = 0
    size: int = 0
    path: str | None = None
    set_digest: str = ""
    skipped: list[dict[str, str]] = field(default_factory=list)
    metadata_only: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)


def _new_id() -> str:
    return "vb-" + bc.utcnow().strftime("%Y%m%dT%H%M%S") + "-" + _secrets.token_hex(3)


def _recipient_or_passphrase(state_dir: Path, passphrase: Unlock) -> tuple[C.Recipient | None, str | None]:
    if passphrase is not None:
        return None, _unlock_value(passphrase)
    rec = load_recipient(state_dir)
    if rec is None:
        raise NotConfigured(
            "no backup key is configured, so a backup cannot be encrypted without a person present. "
            f"Nothing was written. Set one up: `{_cli()} vault key-init`."
        )
    return rec, None


def create_backup(
    state_dir: Path,
    *,
    root: Path | None = None,
    reason: str = "manual",
    trigger: str = "manual",
    passphrase: Unlock = None,
    force: bool = False,
    store: Any = None,
    policy: BackupPolicy | None = None,
) -> BackupResult:
    state_dir = Path(state_dir)
    policy = policy or load_policy(state_dir)
    recipient, pw = _recipient_or_passphrase(state_dir, passphrase)
    bstore = BackupStore(Path(root) if root else default_root(state_dir))

    got = S.collect(state_dir, extra_files=policy.extra_files, store=store)
    result = BackupResult(status="created", skipped=got.skipped,
                          metadata_only=[m["name"] for m in got.metadata_only], set_digest=got.set_digest())
    if not got.entries:
        result.status = "empty"
        result.warnings.append("nothing to back up: the vault holds no values in scope")
        return result

    with bstore.locked():
        recs, _ = _fold(bstore)
        live = sorted((r for r in recs.values() if not r.deleted), key=lambda r: (r.seq, r.created_at, r.id))
        if live and not force:
            newest = live[-1]
            if newest.set_digest == result.set_digest and _inspect(bstore, newest)["status"] == "ok":
                result.status, result.id, result.count = "unchanged", newest.id, newest.count
                return result

        bid, now = _new_id(), bc.iso(bc.utcnow())
        header_fields = {"id": bid, "created_at": now, "host": socket.gethostname(),
                         "axiom_version": ss._axiom_version(), "entries": got.manifest()}
        payload = {e.name: {"kind": e.kind, "value": e.value, "metadata": e.metadata} for e in got.entries}
        sealed = C.seal(payload, header_fields, recipient=recipient, passphrase=pw)

        path = bstore.write_archive(bid, sealed.archive)
        # Verify-on-write: read it BACK from disk and decrypt it in memory.
        try:
            body = C.open_with_content_key(path.read_bytes(), sealed.content_key)
            ok = {n: e["value"] for n, e in body["entries"].items()} == {e.name: e.value for e in got.entries}
        except (OSError, C.VaultBackupError):
            ok = False
        if not ok:
            try:
                path.unlink()
            except OSError:
                pass
            raise C.VaultBackupError("verify-on-write failed: the archive on disk did not decrypt to the "
                                     "vault contents; it was removed and nothing was recorded")
        bstore.ledger_append("backup", bid, {
            "sha256": bc.sha256_bytes(sealed.archive), "size": len(sealed.archive), "count": len(got.entries),
            "set_digest": result.set_digest, "reason": reason, "trigger": trigger, "created_at": now,
            "host": header_fields["host"],
        })
        record_head(state_dir, bstore)
        result.id, result.count, result.size, result.path = bid, len(got.entries), len(sealed.archive), str(path)
        result.names = [e.name for e in got.entries]
        _replicate(state_dir, bstore, policy, result.warnings)
    return result


def _clear_pending(state_dir: Path) -> None:
    try:
        pending_path(state_dir).unlink()
    except OSError:
        pass


# ===========================================================================
# Second copy
# ===========================================================================


def _replicate(state_dir: Path, store: BackupStore, policy: BackupPolicy, warnings: list[str]) -> None:
    """Mirror archives + ledger to ``vault.backup.second_copy``.  Failure is a warning, never a loss."""
    if not policy.second_copy:
        return
    second = BackupStore(Path(policy.second_copy).expanduser())
    try:
        second.ensure()
        want = set(store.archive_ids_on_disk())
        for bid in want:
            src = store.read_archive(bid)
            dst = second.archive_path(bid)
            if not dst.exists() or dst.read_bytes() != src:
                second.write_archive(bid, src)
        for bid in set(second.archive_ids_on_disk()) - want:
            second.archive_path(bid).unlink()
        bc.atomic_publish(second.ledger, store.ledger.read_bytes() if store.ledger.exists() else b"")
        record_head(state_dir, second)
    except (OSError, C.VaultBackupError) as exc:
        warnings.append(f"second copy to {policy.second_copy} FAILED ({type(exc).__name__}); the primary is intact")


# ===========================================================================
# Verify
# ===========================================================================


@dataclass
class VerifyReport:
    ok: bool
    problems: list[str] = field(default_factory=list)
    checked: int = 0
    deep: bool = False
    stray_temp: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)


def verify(state_dir: Path, root: Path, *, ref: str | None = None, unlock: Unlock = None) -> VerifyReport:
    """Without ``unlock``: ledger chain, rollback evidence, file hashes, headers.
    With it: also decrypt every archive and compare each fingerprint with its manifest."""
    store = BackupStore(root)
    rep = VerifyReport(ok=True, deep=unlock is not None)
    lc = verify_ledger(store)
    rep.problems += lc.problems
    rep.problems += check_head(Path(state_dir), store)
    recs, _ = _fold(store)
    targets = [resolve_id(root, ref)] if ref else [r.id for r in recs.values() if not r.deleted]
    for bid in sorted(targets):
        rep.checked += 1
        insp = _inspect(store, recs[bid])
        if insp["status"] != "ok":
            rep.problems.append(f"{bid}: {insp['status']}" + (f" ({insp['detail']})" if insp.get("detail") else ""))
            continue
        if unlock is not None:
            try:
                header, body = C.open_archive(store.read_archive(bid), unlock=_unlock_value(unlock))
            except C.VaultBackupError as exc:
                rep.problems.append(f"{bid}: {exc}")
                continue
            for m in header["entries"]:
                if m.get("scope") != "value":
                    continue
                e = body["entries"].get(m["name"])
                if e is None or S.fingerprint_of(e["value"]) != m["fingerprint"]:
                    rep.problems.append(f"{bid}: entry {m['name']!r} does not match its manifest fingerprint")
    on_disk = set(store.archive_ids_on_disk())
    rep.orphans = sorted(on_disk - set(recs))
    for o in rep.orphans:
        rep.problems.append(f"{o}: archive file is not in the ledger (unrecorded; do not trust)")
    rep.stray_temp = store.stray_temp_files()
    rep.ok = not rep.problems
    return rep


# ===========================================================================
# Prune
# ===========================================================================


@dataclass
class PruneOutcome:
    plan: bc.RetentionPlan
    applied: bool
    deleted: list[str] = field(default_factory=list)


def prune(state_dir: Path, root: Path, *, apply: bool = False, policy: BackupPolicy | None = None) -> PruneOutcome:
    policy = policy or load_policy(Path(state_dir))
    store = BackupStore(root)
    with store.locked():
        rows = list_backups(root)
        infos = [bc.BackupInfo(id=r["id"], created_at=bc.parse_iso(r["created_at"]), size_bytes=r["size"],
                                 verified=bool(r["verified"]), pinned=r["pinned"]) for r in rows]
        plan = bc.plan_retention(infos, policy.retention, bc.utcnow())
        out = PruneOutcome(plan=plan, applied=False)
        if not apply:
            return out
        ids = [d.id for d in plan.delete]
        for bid in ids:
            try:
                store.archive_path(bid).unlink()
            except FileNotFoundError:
                pass
        if ids:
            store.ledger_append("prune", None, {"ids": ids})
            record_head(Path(state_dir), store)
        store.sweep_stray_temp()
        out.applied, out.deleted = True, ids
        warnings: list[str] = []
        _replicate(Path(state_dir), store, policy, warnings)
        return out


def set_pinned(root: Path, ref: str, pinned: bool, state_dir: Path | None = None) -> str:
    store = BackupStore(root)
    bid = resolve_id(root, ref)
    with store.locked():
        store.ledger_append("pin" if pinned else "unpin", bid, {})
        if state_dir:
            record_head(Path(state_dir), store)
    return bid


# ===========================================================================
# Taint / scrub (interplay with exposure and rotation)
# ===========================================================================


def taint(state_dir: Path, root: Path, name: str, fingerprint: str, *, where: str = "") -> list[str]:
    """Mark one (name, value-fingerprint) as exposed.  Key-free.  Returns the backup ids that hold it."""
    store = BackupStore(root)
    with store.locked():
        store.ledger_append("taint", None, {"name": name, "fingerprint": fingerprint, "where": where})
        record_head(Path(state_dir), store)
    return [r["id"] for r in list_backups(root) if name in r["tainted"]]


def taint_exposed(state_dir: Path, name: str, *, where: str = "", root: Path | None = None) -> list[str] | None:
    """Called by ``secrets exposed``: taint the credential's CURRENT value (fingerprint from the
    metadata index; no value is read).  ``None`` when there is nothing to do."""
    state_dir = Path(state_dir)
    root = Path(root) if root else default_root(state_dir)
    if not (root / "ledger.jsonl").exists():
        return None
    try:
        fp = S.foreign_store(state_dir).metadata(name).get("fingerprint")
    except (KeyError, ValueError, OSError):
        return None
    if not fp:
        return None
    return taint(state_dir, root, name, fp, where=where)


@dataclass
class ScrubOutcome:
    tainted_ids: list[str] = field(default_factory=list)
    rewritten: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    purged: bool = False


def scrub(state_dir: Path, root: Path, name: str, *, unlock: Unlock = None, all_versions: bool = False,
          policy: BackupPolicy | None = None) -> ScrubOutcome:
    """Remove an exposed credential's old value from past backups.

    Without ``unlock`` there is nothing to decrypt with, so backups holding the exposed value are
    only excluded from ``latest`` and withheld on restore.  With it, each affected archive is
    decrypted, the entry dropped, and the archive re-encrypted to the same recipient, verified in
    memory, and swapped in atomically.  The ledger records old and new hashes, never the value."""
    state_dir = Path(state_dir)
    policy = policy or load_policy(state_dir)
    store = BackupStore(Path(root))
    out = ScrubOutcome()
    recs, taints = _fold(store)
    tk = _taint_keys(taints)
    affected: list[str] = []
    for r in recs.values():
        if r.deleted:
            continue
        header = _inspect(store, r)["header"]
        if not header:
            continue
        hit = [m for m in header.get("entries", []) if m.get("name") == name
               and (all_versions or (m.get("name"), m.get("fingerprint")) in tk)]
        if hit:
            affected.append(r.id)
    out.tainted_ids = sorted(affected)
    if unlock is None or not affected:
        return out
    secret = _unlock_value(unlock)
    with store.locked():
        for bid in sorted(affected):
            try:
                data = store.read_archive(bid)
                header, body = C.open_archive(data, unlock=secret)
                body["entries"].pop(name, None)
                enc = header["enc"]
                fields = {k: v for k, v in header.items() if k not in ("enc", "format", "count")}
                fields["entries"] = [m for m in header["entries"] if m.get("name") != name]
                if enc["mode"] == "recipient":
                    rec = C.Recipient(public=C._b64d(enc["recipient_pub"]), wrapped_priv=enc["wrapped_priv"],
                                      label=enc["wrapped_priv"].get("label", "recovery-code"))
                    sealed = C.seal(body["entries"], fields, recipient=rec)
                else:
                    sealed = C.seal(body["entries"], fields, passphrase=secret)
                check = C.open_with_content_key(sealed.archive, sealed.content_key)
                if name in check["entries"]:
                    raise C.VaultBackupError("scrub verification failed")
                old_sha = bc.sha256_bytes(data)
                store.write_archive(bid, sealed.archive)
                store.ledger_append("scrub", bid, {"names": [name], "old_sha256": old_sha,
                                                   "sha256": bc.sha256_bytes(sealed.archive),
                                                   "size": len(sealed.archive), "count": len(body["entries"])})
                out.rewritten.append(bid)
            except (C.VaultBackupError, OSError):
                out.failed.append(bid)
        record_head(state_dir, store)
        warnings: list[str] = []
        _replicate(state_dir, store, policy, warnings)
    out.purged = bool(out.rewritten) and not out.failed
    return out


# ===========================================================================
# Restore
# ===========================================================================


@dataclass
class PlanItem:
    name: str
    kind: str
    action: str  # add | unchanged | conflict | withheld
    backup_fingerprint: str
    current_fingerprint: str | None = None
    note: str = ""


@dataclass
class RestoreReport:
    id: str
    applied: bool
    items: list[PlanItem] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    skipped_conflicts: list[str] = field(default_factory=list)
    withheld: list[str] = field(default_factory=list)
    failed_verification: list[str] = field(default_factory=list)
    pre_restore_id: str | None = None
    warnings: list[str] = field(default_factory=list)
    metadata_only: list[str] = field(default_factory=list)
    #: name -> why it could not be written (the exception TYPE only: a backend's text is not ours to print)
    not_written: dict[str, str] = field(default_factory=dict)
    #: newer backups ``latest`` passed over -> why
    skipped_backups: dict[str, list[str]] = field(default_factory=dict)
    #: human lines about how the backup was chosen (fell back, second copy used)
    notes: list[str] = field(default_factory=list)
    root: str = ""


def _selected(names: Iterable[str], only: Iterable[str]) -> list[str]:
    only = list(only)
    if not only:
        return sorted(names)
    return sorted(n for n in names if any(fnmatchcase(n, pat) for pat in only))


DECRYPT_HELP = (
    "If the recovery code or passphrase is the problem: re-enter the one saved when the key was created "
    "(`{cli} vault key-info` shows which key made which backups). If it is lost, no one can open these "
    "backups: re-issue the credentials, then `{cli} vault key-init --force` and `{cli} vault backup` for a new key. "
    "Another copy of the backups (a second copy, another disk) is worth trying: "
    "`{cli} vault restore latest --from DIR`."
)


class VaultKind:
    """:class:`~axiom.infra.backup_core.BackupKind` over a vault backup directory.

    ``check`` is the key-free integrity check and, when a secret is given, an actual decryption
    plus a fingerprint comparison: a backup that "verifies" but cannot be opened with the key
    in hand is not one to restore from.  Opened archives are cached for the restore that follows."""

    noun = "vault backup"

    def __init__(self, root: Path, secret: str | None = None, *, first_bad: int | None = None,
                 first_bad_why: str = "") -> None:
        self.root = Path(root)
        self.secret = secret
        self.first_bad, self.first_bad_why = first_bad, first_bad_why
        self.opened: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self._rows: dict[str, dict[str, Any]] | None = None

    def _row_map(self) -> dict[str, dict[str, Any]]:
        if self._rows is None:
            self._rows = {r["id"]: r for r in list_backups(self.root)}
        return self._rows

    def candidates(self) -> list[str]:
        return [r["id"] for r in list_backups(self.root) if not r["tainted"] and r["trigger"] != "restore"]

    def check(self, bid: str) -> list[str]:
        row = self._row_map().get(bid)
        if row is None:
            return ["not in the ledger"]
        if row["status"] != "ok":
            return [row.get("detail") or row["status"]]
        if self.first_bad is not None and row["seq"] >= self.first_bad:
            return [f"its ledger entry ({row['seq']}) is past a break in the ledger chain (entry {self.first_bad}: "
                    f"{self.first_bad_why}), so the ledger cannot vouch for it"]
        if self.secret is None:
            return []
        try:
            header, body = C.open_archive(BackupStore(self.root).read_archive(bid), unlock=self.secret)
        except (C.VaultBackupError, OSError) as exc:
            return [str(exc) if isinstance(exc, C.VaultBackupError) else "unreadable"]
        for m in header["entries"]:
            if m.get("scope") != "value":
                continue
            e = body["entries"].get(m["name"])
            if e is None or S.fingerprint_of(e["value"]) != m["fingerprint"]:
                return ["an entry does not match its manifest fingerprint"]
        self.opened[bid] = (header, body)
        return []


def _no_usable_message(root: Path, rejected: Iterable[bc.Rejection]) -> str:
    c = _cli()
    rej = list(rejected)
    lines = [f"no vault backup in {root} can be used, so nothing was changed. Why each failed:"]
    lines += [f"  {r.id}: {r.reasons[0]}" for r in rej]
    if any(r.reasons and r.reasons[0] == C.GENERIC_FAILURE for r in rej):
        lines.append(DECRYPT_HELP.format(cli=c))
    lines.append(
        f"Try: another location or a second copy (`{c} vault restore latest --from DIR`); or the emergency "
        "script, which needs nothing from Axiom (`python3 -I axi-restore.py vault verify DIR --code-file FILE`)."
    )
    return "\n".join(lines)


def restore(
    state_dir: Path,
    root: Path,
    ref: str,
    *,
    unlock: Unlock,
    only: Iterable[str] = (),
    apply: bool = False,
    overwrite: bool = False,
    include_tainted: bool = False,
    skip_pre_backup: bool = False,
    force: bool = False,
    store: Any = None,
) -> RestoreReport:
    """Dry run unless ``apply``.

    ``latest`` is the newest backup that VERIFIES AND OPENS with the key in hand; newer ones that
    do not are passed over and named (``rep.skipped_backups``).  If the primary directory has no
    usable backup and ``vault.backup.second_copy`` does, the second copy is used and the report
    says so.  Raises :class:`NoUsableBackup` (nothing changed) when nothing can be used."""
    state_dir, root = Path(state_dir), Path(root)
    secret = _unlock_value(unlock)
    policy = load_policy(state_dir)
    try:
        return _restore_from(state_dir, root, ref, secret, policy, only, apply, overwrite, include_tainted,
                             skip_pre_backup, force, store)
    except NoSuchBackup as first:
        second = Path(policy.second_copy).expanduser() if policy.second_copy else None
        if second is None or second.resolve() == root.resolve():
            raise
        try:
            rep = _restore_from(state_dir, second, ref, secret, policy, only, apply, overwrite, include_tainted,
                                skip_pre_backup, force, store)
        except NoSuchBackup:
            raise first from None
        rep.notes.insert(0, f"USED THE SECOND COPY {second}: the primary {root} had no usable backup "
                            f"({str(first).splitlines()[0]})")
        return rep


def _restore_from(state_dir, root, ref, secret, policy, only, apply, overwrite, include_tainted,
                  skip_pre_backup, force, store) -> RestoreReport:
    bstore = BackupStore(root)
    notes: list[str] = []
    head_problems = check_head(state_dir, bstore)
    chain = verify_ledger(bstore)
    kind = VaultKind(root, secret, first_bad=chain.first_bad, first_bad_why=chain.problems[0] if chain.problems else "")
    skipped: dict[str, list[str]] = {}
    if ref == "latest":
        if not list_backups(root):
            resolve_id(root, "latest")  # raises NoSuchBackup with the precise reason when there is nothing at all
        if head_problems and not force:
            raise NoUsableBackup(
                "\n".join(head_problems) + "\nREFUSING to guess which backup is the latest from a directory that "
                f"may have been rolled back or emptied; nothing was changed. Name one (`{_cli()} vault ls`, then "
                f"`{_cli()} vault restore <id>`), or --force."
            )
        sel = bc.select_newest_intact(kind)
        if sel.chosen is None:
            if sel.rejected and all(r.reasons == [C.GENERIC_FAILURE] for r in sel.rejected):
                raise C.DecryptionError()  # every backup is intact but none opens: the code is the problem
            raise NoUsableBackup(_no_usable_message(root, sel.rejected))
        bid = sel.chosen
        skipped = {r.id: r.reasons for r in sel.rejected}
        notes += [f"FELL BACK: {ln}" for ln in sel.explain("vault backup")]
        header, body = kind.opened[bid]
    else:
        bid = resolve_id(root, ref)
        notes += [f"WARNING: {p}" for p in [*head_problems, *chain.problems]]
        header, body = C.open_archive(bstore.read_archive(bid), unlock=secret)
    _, taints = _fold(bstore)
    tk = _taint_keys(taints)
    manifest = {m["name"]: m for m in header["entries"]}
    rep = RestoreReport(id=bid, applied=False, notes=notes, skipped_backups=skipped, root=str(root))
    rep.metadata_only = sorted(n for n, m in manifest.items() if m.get("scope") != "value")
    current = S.current_fingerprints(state_dir, extra_files=policy.extra_files, store=store)
    for name in _selected(body["entries"], only):
        entry, m = body["entries"][name], manifest.get(name, {})
        fp = S.fingerprint_of(entry["value"])
        now = current.get(name)
        item = PlanItem(name=name, kind=entry["kind"], action="add", backup_fingerprint=fp, current_fingerprint=now)
        if (name, m.get("fingerprint")) in tk and not include_tainted:
            item.action, item.note = "withheld", "this value was reported exposed and rotated; it is not restored"
            rep.withheld.append(name)
        elif now is None:
            item.action = "add"
        elif now == fp:
            item.action = "unchanged"
        else:
            item.action = "conflict"
            item.note = "a different value is in the vault now"
            if not overwrite:
                rep.skipped_conflicts.append(name)
        if (name, m.get("fingerprint")) in tk and include_tainted:
            rep.warnings.append(
                f"WARNING: {name!r} was reported EXPOSED and rotated. Restoring this old value puts a leaked "
                "credential back in service. Rotate it again as soon as the restore completes."
            )
        rep.items.append(item)
    if not apply:
        return rep

    todo = [i for i in rep.items if i.action == "add" or (i.action == "conflict" and overwrite)]
    if todo and current and not skip_pre_backup:
        try:
            pre = create_backup(state_dir, root=root, reason=f"pre-restore of {bid}", trigger="restore",
                                force=True, store=store, policy=policy)
        except NotConfigured:
            raise NotConfigured(
                "the vault is not empty and no backup key is configured, so the required pre-restore "
                f"backup cannot be made. Run `{_cli()} vault key-init` first. Nothing was changed."
            ) from None
        except (C.VaultBackupError, OSError) as exc:
            why = str(exc) if isinstance(exc, C.VaultBackupError) else (exc.strerror or type(exc).__name__)
            raise PreRestoreFailed(
                f"the safety backup before restoring could not be made ({why}), so NOTHING was restored. "
                f"Fix the cause (disk space? permissions?), or pass --skip-pre-backup to restore without a safety net."
            ) from None
        rep.pre_restore_id = pre.id
    with bstore.locked():
        for i in todo:
            e = body["entries"][i.name]
            try:
                if not bc.FAULTS.hit("restore.file", path=i.name):  # a drill can make a write silently not land
                    S.write_entry(state_dir, i.name, e["kind"], e["value"], e.get("metadata") or {}, store=store)
                rep.written.append(i.name)
            except Exception as exc:  # noqa: BLE001 -- the TYPE only; a backend's text may echo a value
                rep.not_written[i.name] = (exc.strerror if isinstance(exc, OSError) and exc.strerror
                                           else type(exc).__name__)
        after = S.current_fingerprints(state_dir, extra_files=policy.extra_files, store=store)
        for i in todo:
            if i.name in rep.written and not _landed(after, i):
                rep.failed_verification.append(i.name)
        bstore.ledger_append("restore", bid, {"names": rep.written, "pre_restore_id": rep.pre_restore_id,
                                              "failed_verification": rep.failed_verification,
                                              "not_written": sorted(rep.not_written)})
        record_head(state_dir, bstore)
    rep.applied = True
    if rep.failed_verification:
        rep.warnings.append("VERIFICATION FAILED for: " + ", ".join(rep.failed_verification))
    return rep


def _landed(after: dict[str, str], item: PlanItem) -> bool:
    """Did the value we wrote actually land?  Compared by fingerprint, read back from the vault."""
    return after.get(item.name) == item.backup_fingerprint


# ===========================================================================
# Triggers: scheduled tick and change hook
# ===========================================================================


@dataclass
class TickResult:
    ok: bool
    status: str
    message: str
    backup: BackupResult | None = None
    pruned: list[str] = field(default_factory=list)


def _newest_age(root: Path) -> timedelta | None:
    rows = [r for r in list_backups(root) if r["verified"]]
    if not rows:
        return None
    age = bc.utcnow() - bc.parse_iso(rows[0]["created_at"])
    # A backup dated in the FUTURE (the clock was wrong when it was taken) says nothing about how
    # long ago it was: treat it as no trustworthy backup, so the next one is due rather than
    # skipped until the clock catches up.
    return None if age < -bc._FUTURE_TOLERANCE else age


def _pending_age(state_dir: Path) -> timedelta | None:
    try:
        ts = json.loads(pending_path(state_dir).read_text("utf-8"))["ts"]
        return bc.utcnow() - bc.parse_iso(ts)
    except (OSError, ValueError, KeyError):
        return None


def _mark_pending(state_dir: Path, reason: str) -> None:
    if _pending_age(state_dir) is not None:
        return
    bc.mkdir_private(pending_path(state_dir).parent)
    bc.atomic_publish(pending_path(state_dir), json.dumps({"ts": bc.iso(bc.utcnow()), "reason": reason}).encode())


def tick(state_dir: Path, *, root: Path | None = None, force: bool = False, store: Any = None) -> TickResult:
    """Scheduled backup.  Never prompts.  Without a recipient it says so and writes nothing."""
    state_dir = Path(state_dir)
    policy = load_policy(state_dir)
    root = Path(root) if root else default_root(state_dir)
    if not policy.enabled:
        return TickResult(True, "disabled", "vault backups are disabled (vault.backup.enabled=false)")
    if load_recipient(state_dir) is None:
        return TickResult(False, "not-configured",
                          "scheduled vault backup cannot run: no backup key is configured and a scheduled run "
                          "has no person to ask for a passphrase. NOTHING was written (and no plaintext backup "
                          f"will ever be). Set one up once, interactively: `{_cli()} vault key-init`.")
    age = _newest_age(root)
    pend = _pending_age(state_dir)
    due_interval = age is None or age >= timedelta(hours=policy.interval_hours) * 0.9
    due_pending = pend is not None and pend >= timedelta(seconds=policy.debounce_seconds)
    if not (force or due_interval or due_pending):
        return TickResult(True, "not-due", f"newest backup is {age} old; interval is {policy.interval_hours}h")
    try:
        res = create_backup(state_dir, root=root, reason="scheduled", trigger="schedule", store=store, policy=policy)
    except C.VaultBackupError as exc:
        log.error("SCHEDULED VAULT BACKUP FAILED: %s", exc)
        return TickResult(False, "failed", f"scheduled vault backup failed: {exc}")
    _clear_pending(state_dir)
    out = TickResult(True, res.status, f"vault backup {res.status}", backup=res)
    if policy.auto_prune and res.status == "created":
        try:
            out.pruned = prune(state_dir, root, apply=True, policy=policy).deleted
        except C.VaultBackupError as exc:
            out.message += f" (prune skipped: {exc})"
    v = verify(state_dir, root)
    if not v.ok:
        out.ok = False
        out.message += "; STORE FAILED VERIFICATION: " + "; ".join(v.problems[:3])
        log.error("%s", out.message)
    return out


def note_change(state_dir: Path, reason: str, *, store: Any = None) -> str:
    """Hook for ``set`` / ``rotate`` / ``rm`` / ``exposed``.  Debounced; never raises; never prompts.

    Returns ``backed-up`` | ``deferred`` | ``unchanged`` | ``not-configured`` | ``disabled`` | ``error``.
    """
    try:
        state_dir = Path(state_dir)
        policy = load_policy(state_dir)
        if not policy.enabled:
            return "disabled"
        if load_recipient(state_dir) is None:
            return "not-configured"
        root = default_root(state_dir)
        age = _newest_age(root)
        if age is not None and age < timedelta(seconds=policy.debounce_seconds):
            _mark_pending(state_dir, reason)
            return "deferred"
        res = create_backup(state_dir, root=root, reason=reason, trigger="change", store=store, policy=policy)
        _clear_pending(state_dir)
        return "unchanged" if res.status == "unchanged" else "backed-up"
    except Exception as exc:  # noqa: BLE001 -- a backup problem must never fail the command that changed the vault
        log.warning("vault backup after %s did not run (%s)", reason, type(exc).__name__)
        return "error"
