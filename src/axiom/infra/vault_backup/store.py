# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""On-disk layout, the hash-chained ledger, the lock, atomic writes.

::

    <root>/                 0700
      archives/<id>.axvb    0600   one encrypted archive per backup
      ledger.jsonl          0600   append-only, hash-chained: backup/scrub/taint/prune/restore/pin
      .lock                 advisory flock; one writer at a time

The ledger is the tamper and rollback evidence.  Each entry's hash covers the
previous hash, and the newest ``(seq, head)`` is also recorded OUTSIDE the root
(``<state>/secrets/vault-backup-head.json``), so restoring an old copy of the
backup directory is detectable: the ledger would be shorter than, or disagree
with, what this host last wrote.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import Any

from axiom.infra import backup_core as bc

from .crypto import VaultBackupError

ARCHIVE_SUFFIX = ".axvb"

LedgerCheck = bc.LedgerCheck


class BackupBusy(VaultBackupError):
    """Another backup/prune/scrub/restore holds the lock."""


class DiskPressureError(VaultBackupError):
    """Not enough free space to write safely; nothing was written."""


class BackupStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.archives = self.root / "archives"
        self.ledger = self.root / "ledger.jsonl"
        self.lockfile = self.root / ".lock"
        self._chain = bc.HashChainLedger(self.ledger, ensure=self.ensure)

    def ensure(self) -> None:
        for d in (self.root, self.archives):
            bc.mkdir_private(d)

    def locked(self, timeout: float = 10.0) -> contextlib.AbstractContextManager[None]:
        return bc.backup_lock(
            self.lockfile, timeout=timeout, prepare=self.ensure,
            busy=lambda: BackupBusy("another vault backup operation is running (lock held); try again shortly"),
        )

    # -- archives -------------------------------------------------------------
    def archive_path(self, bid: str) -> Path:
        if not bid or "/" in bid or bid.startswith(".") or "\\" in bid:
            raise ValueError("bad backup id")
        return self.archives / f"{bid}{ARCHIVE_SUFFIX}"

    def write_archive(self, bid: str, data: bytes) -> Path:
        """Atomic: temp file, fsync, 0600, rename.  The old file is untouched on any failure."""
        self.ensure()
        need = len(data) * 2 + 1024 * 1024
        free, _total = bc.free_bytes(self.root)
        if free < need:
            raise DiskPressureError(
                f"only {free} bytes free where {need} are needed; nothing was written and the "
                "existing backups are untouched"
            )
        dest = self.archive_path(bid)
        try:
            bc.FAULTS.hit("backup.publish", id=bid)
            bc.atomic_publish(dest, data)
        except OSError as exc:
            raise DiskPressureError(
                f"writing the backup failed ({exc.strerror or exc.errno}); the existing backups are "
                "untouched"
            ) from exc
        return dest

    def read_archive(self, bid: str) -> bytes:
        return self.archive_path(bid).read_bytes()

    def archive_ids_on_disk(self) -> list[str]:
        if not self.archives.is_dir():
            return []
        return sorted(p.name[: -len(ARCHIVE_SUFFIX)] for p in self.archives.iterdir()
                      if p.name.endswith(ARCHIVE_SUFFIX) and not p.name.startswith("."))

    def stray_temp_files(self) -> list[str]:
        try:
            if not self.archives.is_dir():
                return []
            return sorted(p.name for p in self.archives.iterdir() if p.name.startswith(".tmp-"))
        except PermissionError:
            # A housekeeping scan cannot see debris in a directory it may not read. Saying nothing here is
            # right: the access error is reported where backups are actually read, with its real reason.
            return []

    def sweep_stray_temp(self, older_than_s: float = 3600.0) -> int:
        n = 0
        for name in self.stray_temp_files():
            p = self.archives / name
            with contextlib.suppress(OSError):
                if time.time() - p.stat().st_mtime > older_than_s:
                    p.unlink()
                    n += 1
        return n

    # -- ledger ----------------------------------------------------------------
    def ledger_entries(self) -> list[dict[str, Any]]:
        return self._chain.entries()

    def ledger_append(self, event: str, bid: str | None, detail: dict[str, Any]) -> dict[str, Any]:
        return self._chain.append(event, bid, detail)


def verify_ledger(store: BackupStore) -> LedgerCheck:
    return store._chain.verify()


# -- head pointer (outside the root) ------------------------------------------


def _head(state_dir: Path) -> bc.ExternalHead:
    from .policy import head_path

    return bc.ExternalHead(head_path(state_dir))


def record_head(state_dir: Path, store: BackupStore) -> None:
    _head(state_dir).record(store.root, store._chain)


def check_head(state_dir: Path, store: BackupStore) -> list[str]:
    """Rollback evidence: the ledger must contain what this host last wrote."""
    return _head(state_dir).check(store.root, store._chain)
