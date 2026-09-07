# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Backup and restore for Axiom PostgreSQL databases.

Provides pg_dump/pg_restore wrappers with timestamped outputs,
auto-backup hooks for RAG upgrades, and restore with confirmation.

Usage::

    from axiom.infra.backup import create_backup, restore_backup

    result = create_backup("postgresql://...", output_dir=Path("~/.axi/backups"))
    restore_backup(result.backup_path, "postgresql://...", confirm=True)
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_BACKUP_DIR = Path.home() / ".axi" / "backups"

# fmt → (pg_dump extra flags, file extension). ``plain`` is the original
# psql-replayable SQL script; ``custom`` is pg_dump's -Fc archive, which
# ``pg_restore --list`` can structurally verify and selectively restore —
# the format the productized backup path standardizes on.
_FORMATS: dict[str, tuple[list[str], str]] = {
    "plain": ([], ".sql"),
    "custom": (["-Fc"], ".dump"),
}


@dataclass
class BackupResult:
    """Result of a backup operation."""

    backup_path: Path
    size_bytes: int
    created_at: str
    database_url: str
    success: bool
    error: str | None = None


def create_backup(
    database_url: str,
    output_dir: Path | None = None,
    label: str = "",
    schemas: list[str] | None = None,
    fmt: str = "plain",
    _dry_run: bool = False,
) -> BackupResult:
    """Create a pg_dump backup of the database.

    Args:
        database_url: PostgreSQL connection string
        output_dir: Directory for backup files (default: ~/.axi/backups/)
        label: Optional label appended to filename
        schemas: Optional schema names to scope the dump (``-n`` flags);
            ``None`` dumps the whole database
        fmt: ``"plain"`` (psql-replayable .sql, the default) or
            ``"custom"`` (pg_dump -Fc .dump — verifiable via
            ``pg_restore --list``)
        _dry_run: If True, create an empty file without calling pg_dump (for testing)

    Returns:
        BackupResult with path and metadata
    """
    if fmt not in _FORMATS:
        raise ValueError(f"unknown backup fmt {fmt!r}; expected one of {sorted(_FORMATS)}")
    fmt_flags, extension = _FORMATS[fmt]

    output_dir = output_dir or _DEFAULT_BACKUP_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    suffix = f"-{label}" if label else ""
    filename = f"axiom-backup-{timestamp}{suffix}{extension}"
    backup_path = output_dir / filename

    if _dry_run:
        if fmt == "custom":
            # A tiny stand-in blob — enough for exists/size assertions.
            backup_path.write_bytes(b"PGDMP dry run backup\n")
        else:
            backup_path.write_text("-- dry run backup\n", encoding="utf-8")
        return BackupResult(
            backup_path=backup_path,
            size_bytes=backup_path.stat().st_size,
            created_at=datetime.now(UTC).isoformat(),
            database_url=database_url,
            success=True,
        )

    cmd = ["pg_dump", database_url, *fmt_flags]
    for schema in schemas or []:
        cmd += ["-n", schema]
    cmd += ["-f", str(backup_path)]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )

        if result.returncode != 0:
            return BackupResult(
                backup_path=backup_path,
                size_bytes=0,
                created_at=datetime.now(UTC).isoformat(),
                database_url=database_url,
                success=False,
                error=result.stderr,
            )

        size = backup_path.stat().st_size
        log.info("Backup created: %s (%d bytes)", backup_path, size)

        return BackupResult(
            backup_path=backup_path,
            size_bytes=size,
            created_at=datetime.now(UTC).isoformat(),
            database_url=database_url,
            success=True,
        )

    except FileNotFoundError:
        return BackupResult(
            backup_path=backup_path,
            size_bytes=0,
            created_at=datetime.now(UTC).isoformat(),
            database_url=database_url,
            success=False,
            error="pg_dump not found — install PostgreSQL client tools",
        )
    except subprocess.TimeoutExpired:
        return BackupResult(
            backup_path=backup_path,
            size_bytes=0,
            created_at=datetime.now(UTC).isoformat(),
            database_url=database_url,
            success=False,
            error="pg_dump timed out after 600s",
        )


def restore_backup(
    backup_path: Path,
    database_url: str,
    confirm: bool = False,
) -> BackupResult:
    """Restore a database from a pg_dump backup.

    Args:
        backup_path: Path to the .sql backup file
        database_url: PostgreSQL connection string
        confirm: Must be True to proceed (safety gate)

    Returns:
        BackupResult with status

    Raises:
        ValueError: If confirm is False
        FileNotFoundError: If backup file doesn't exist
    """
    if not confirm:
        raise ValueError("Restore requires confirm=True — this will overwrite the current database")

    backup_path = Path(backup_path)
    if not backup_path.exists():
        raise FileNotFoundError(f"Backup file not found: {backup_path}")

    try:
        result = subprocess.run(
            ["psql", database_url, "-f", str(backup_path)],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )

        success = result.returncode == 0
        if success:
            log.info("Restore complete from %s", backup_path)
        else:
            log.error("Restore failed: %s", result.stderr)

        return BackupResult(
            backup_path=backup_path,
            size_bytes=backup_path.stat().st_size,
            created_at=datetime.now(UTC).isoformat(),
            database_url=database_url,
            success=success,
            error=result.stderr if not success else None,
        )

    except FileNotFoundError:
        raise RuntimeError("psql not found — install PostgreSQL client tools") from None


def list_backups(backup_dir: Path | None = None) -> list[BackupResult]:
    """List available backups (both plain .sql and custom .dump), newest first."""
    backup_dir = backup_dir or _DEFAULT_BACKUP_DIR
    if not backup_dir.exists():
        return []

    files = [
        f for _flags, ext in _FORMATS.values() for f in backup_dir.glob(f"axiom-backup-*{ext}")
    ]
    backups = []
    for f in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True):
        backups.append(
            BackupResult(
                backup_path=f,
                size_bytes=f.stat().st_size,
                created_at=datetime.fromtimestamp(f.stat().st_mtime, tz=UTC).isoformat(),
                database_url="",
                success=True,
            )
        )
    return backups
