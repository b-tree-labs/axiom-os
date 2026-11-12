# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Encrypted, restorable backup of the credentials the harness itself holds.

State snapshots (``axiom.infra.state_snapshots``) deliberately never copy
credential values.  This is the other half: values ARE copied, but only into
authenticated-encrypted archives whose key is never stored beside them.
See docs/specs/spec-vault-backup.md.
"""

from . import crypto, engine, policy, sources, store
from .crypto import (
    DecryptionError,
    FormatError,
    UnsupportedVersion,
    VaultBackupError,
    WeakPassphrase,
)
from .engine import (
    BackupBusy,
    DiskPressureError,
    NoSuchBackup,
    NotConfigured,
    NoUsableBackup,
    PreRestoreFailed,
    VaultKind,
    create_backup,
    init_key,
    key_info,
    list_backups,
    load_recipient,
    note_change,
    prune,
    restore,
    scrub,
    show,
    taint_exposed,
    tick,
    verify,
)

__all__ = [
    "BackupBusy", "DecryptionError", "DiskPressureError", "FormatError", "NoSuchBackup", "NotConfigured",
    "NoUsableBackup", "PreRestoreFailed", "VaultKind",
    "UnsupportedVersion", "VaultBackupError", "WeakPassphrase", "create_backup", "crypto",
    "engine", "init_key", "key_info", "list_backups", "load_recipient", "note_change", "policy", "prune",
    "restore", "scrub", "show", "sources", "store", "taint_exposed", "tick", "verify",
]
