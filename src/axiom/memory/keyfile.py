# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The signing key beside a memory scope's audit trail: load it, or create it once.

Five call sites carried a copy of "if node.key exists load it, else generate
one", and all five had the same hole: a key that is MISSING is not the same as
a key that was never made. Every entry in ``audit.jsonl`` is signed by it, so
silently minting a replacement (after a deleted file, a renamed directory, a
restore that missed one file) leaves a trail whose history no longer verifies
and says nothing about why.

The rule here: generate only for a scope that has no signed history. Anything
else that is not a readable key stops, names the file, and says how to repair.
"""

from __future__ import annotations

import os
from pathlib import Path

from axiom.infra.state_integrity import (
    StateDamaged,
    atomic_write_bytes,
    is_dangling_symlink,
    read_state_bytes,
)
from axiom.vega.identity.keypair import Keypair, generate_keypair


def _fix(base: Path, key_path: Path) -> str:
    return (
        f"restore {key_path} from a backup; if it is truly lost, keep the old history "
        f"readable and start a new scope with `mv '{base}' '{base}.old'` "
        "(entries signed by the lost key stay verifiable only with that key)"
    )


def load_or_create_scope_key(base: Path, name: str = "node.key") -> Keypair:
    """Load ``<base>/<name>``; create it only when the scope has no signed history."""
    base = Path(base)
    key_path = base / name
    fix = _fix(base, key_path)
    try:
        base.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError):
        raise StateDamaged(base, "is not a directory (a file or a broken link is in its place)", fix) from None
    if not os.access(base, os.R_OK | os.X_OK | os.W_OK):
        raise StateDamaged(base, "is not accessible (permission denied)", f"run `chmod 700 '{base}'`, or " + fix)

    if os.path.lexists(key_path) or is_dangling_symlink(key_path):
        raw = read_state_bytes(key_path, fix=fix)
        try:
            return Keypair.from_private_bytes(raw or b"")
        except Exception:  # noqa: BLE001 - any failure to parse means it is not a key
            raise StateDamaged(key_path, "is not a valid signing key", fix) from None

    audit = base / "audit.jsonl"
    if audit.exists() and audit.stat().st_size > 0:
        raise StateDamaged(
            key_path,
            "is missing, but audit.jsonl holds entries signed by it. A new key was NOT "
            "generated (those entries would stop verifying)",
            fix,
        )
    kp = generate_keypair()
    atomic_write_bytes(key_path, kp.export_private(), mode=0o600)
    return kp


__all__ = ["load_or_create_scope_key"]
