# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The seam between credential changes and vault backups (see spec-vault-backup.md).

``set`` / ``rm`` / ``rotate`` / ``exposed`` call :func:`after_change` once they succeed.  It is a
no-op unless a backup key has been configured for this state dir, it is debounced, it never
prompts, and it never raises: a backup problem must not fail the command that changed the vault.
"""

from __future__ import annotations

from pathlib import Path


def after_change(state_dir: Path, reason: str) -> str:
    """``backed-up`` | ``deferred`` | ``unchanged`` | ``not-configured`` | ``disabled`` | ``error``."""
    try:
        from axiom.infra.vault_backup import note_change

        return note_change(Path(state_dir), reason)
    except Exception:  # noqa: BLE001
        return "error"


def taint_exposed(state_dir: Path, name: str, where: str) -> list[str] | None:
    """Mark backups that hold ``name``'s current (now exposed) value.  Never raises."""
    try:
        from axiom.infra.vault_backup import taint_exposed as _taint

        return _taint(Path(state_dir), name, where=where)
    except Exception:  # noqa: BLE001
        return None


def hint(result: str) -> list[str]:
    """Action lines worth showing for a hook result (silence is the normal case)."""
    from axiom.infra.state_snapshots import _brand_cli

    c = _brand_cli()
    if result == "not-configured":
        return ["NOTE: this change is not covered by a vault backup (no backup key yet). "
                f"Set one up once: `{c} vault key-init`"]
    if result == "error":
        return [f"WARNING: the automatic vault backup did not run; run `{c} vault backup` and check `{c} vault verify`"]
    return []
