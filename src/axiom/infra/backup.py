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

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from axiom.infra import backup_core as bc

log = logging.getLogger(__name__)

#: A dump is refused when writing it would leave less than this free.
MIN_FREE_BYTES = 64 * 1024 * 1024
_MANIFEST_SUFFIX = ".manifest.json"
_PLAIN_TRAILER = b"PostgreSQL database dump complete"
_DRY_RUN_MARKERS = (b"-- dry run backup", b"PGDMP dry run backup")

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
    exclude_table_data: list[str] | None = None,
    fmt: str = "plain",
    timeout_s: int = 600,
    _dry_run: bool = False,
) -> BackupResult:
    """Create a pg_dump backup of the database.

    Args:
        database_url: PostgreSQL connection string
        output_dir: Directory for backup files (default: ~/.axi/backups/)
        label: Optional label appended to filename
        schemas: Optional schema names to scope the dump (``-n`` flags);
            ``None`` dumps the whole database.

            Scoping by schema is usually the WRONG knob on a real database,
            and the failure is silent until a restore is attempted: pg_dump
            will happily dump a view whose table lives in an excluded schema,
            and the restore then dies on the first such view. Observed here —
            `bronze.documents` selects from `public`, and all seven `gold`
            views select from `silver`, so a "small" scoped dump produced a
            file that passed every structural check and could not be restored.
            Prefer ``exclude_table_data``.
        exclude_table_data: Tables whose DATA is skipped while their
            DEFINITION is kept (``--exclude-table-data``), so dependent views
            still resolve on restore. This is how a database dominated by a
            few enormous, re-derivable tables gets a correct backup that fits:
            everything restores, the bulk simply comes back empty.
            Accepts pg_dump patterns, e.g. ``public.reactor_timeseries_*``.
        timeout_s: How long pg_dump may run. The default suits a small
            database; a large one needs more, and a backup killed by its own
            timeout reports failure loudly rather than truncating silently.
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
    with bc.backup_lock(output_dir / ".lock", busy=lambda: BackupBusy(
            f"another backup or prune is running in {output_dir}; try again shortly")):
        return _create_locked(database_url, output_dir, label, schemas, exclude_table_data, fmt,
                              fmt_flags, extension, timeout_s, _dry_run)


class BackupBusy(RuntimeError):
    """Another backup/prune/restore holds the directory lock."""


def _failed(path: Path, url: str, error: str) -> BackupResult:
    return BackupResult(backup_path=path, size_bytes=0, created_at=datetime.now(UTC).isoformat(),
                        database_url=url, success=False, error=error)


def _discard_partial(path: Path) -> None:
    """pg_dump leaves a truncated file behind when it dies; a listing must never offer it."""
    for p in (path, _manifest_path(path)):
        try:
            p.unlink()
        except OSError:
            pass


def _manifest_path(backup_path: Path) -> Path:
    """Hidden sidecar, so ``axiom-backup-*`` globs keep meaning "the dump files"."""
    return backup_path.with_name("." + backup_path.name + _MANIFEST_SUFFIX)


def delete_backup(backup_path: Path) -> None:
    """Remove one dump and its recorded manifest."""
    _discard_partial(Path(backup_path))


def _record_manifest(path: Path, fmt: str) -> None:
    data = {"sha256": bc.sha256_file(path), "size": path.stat().st_size, "fmt": fmt,
            "created_at": datetime.now(UTC).isoformat()}
    bc.atomic_publish(_manifest_path(path), json.dumps(data, sort_keys=True).encode())


def _libpq_url(url: str) -> str:
    """A URL the PostgreSQL client tools understand.

    The platform's database URLs carry a SQLAlchemy driver (``postgresql+psycopg2://...``). libpq does
    not know that scheme, so ``pg_dump``/``psql``/``pg_restore`` treated the whole string as a database
    NAME and fell back to the default local socket, failing on any machine whose server is not on it
    (every CI runner, and any TCP-only setup).
    """
    return re.sub(r"^postgres(?:ql)?\+\w+://", "postgresql://", url)


def _create_locked(database_url, output_dir, label, schemas, exclude_table_data, fmt, fmt_flags,
                   extension, timeout_s, _dry_run) -> BackupResult:
    ok, why = bc.has_room(output_dir, 0, min_free_bytes=MIN_FREE_BYTES)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    suffix = f"-{label}" if label else ""
    filename = f"axiom-backup-{timestamp}{suffix}{extension}"
    backup_path = output_dir / filename
    if not ok:
        return _failed(backup_path, database_url,
                       f"not enough free space to dump safely ({why}); nothing was written")

    if _dry_run:
        if fmt == "custom":
            # A tiny stand-in blob — enough for exists/size assertions.
            backup_path.write_bytes(b"PGDMP dry run backup\n")
        else:
            backup_path.write_text("-- dry run backup\n", encoding="utf-8")
        _record_manifest(backup_path, fmt)
        return BackupResult(
            backup_path=backup_path,
            size_bytes=backup_path.stat().st_size,
            created_at=datetime.now(UTC).isoformat(),
            database_url=database_url,
            success=True,
        )

    cmd = ["pg_dump", _libpq_url(database_url), *fmt_flags]
    for schema in schemas or []:
        cmd += ["-n", schema]
    for pattern in exclude_table_data or []:
        cmd += [f"--exclude-table-data={pattern}"]
    cmd += ["-f", str(backup_path)]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )

        if result.returncode != 0:
            _discard_partial(backup_path)
            return _failed(backup_path, database_url, result.stderr)

        size = backup_path.stat().st_size
        problems = verify_backup(backup_path)
        if problems:
            _discard_partial(backup_path)
            return _failed(backup_path, database_url, "the dump failed its own check: " + problems[0])
        _record_manifest(backup_path, fmt)
        log.info("Backup created: %s (%d bytes)", backup_path, size)

        return BackupResult(
            backup_path=backup_path,
            size_bytes=size,
            created_at=datetime.now(UTC).isoformat(),
            database_url=database_url,
            success=True,
        )

    except FileNotFoundError:
        _discard_partial(backup_path)
        return _failed(backup_path, database_url, "pg_dump not found — install PostgreSQL client tools")
    except subprocess.TimeoutExpired:
        _discard_partial(backup_path)
        return _failed(backup_path, database_url, f"pg_dump timed out after {timeout_s}s")


def restore_backup(
    backup_path: Path,
    database_url: str,
    confirm: bool = False,
    *,
    verify_first: bool = True,
) -> BackupResult:
    """Restore a database from a pg_dump backup.

    Args:
        backup_path: Path to the ``.sql`` (psql) or ``.dump`` (pg_restore) backup file
        database_url: PostgreSQL connection string
        confirm: Must be True to proceed (safety gate)
        verify_first: Refuse a backup that fails :func:`verify_backup` (truncated, altered,
            empty).  A restore starts by changing the database, so it must not start from a
            file already known to be bad.

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
    if verify_first:
        problems = verify_backup(backup_path)
        if problems:
            return _failed(backup_path, database_url,
                           f"REFUSING to restore {backup_path.name}: {problems[0]}. The database was not touched. "
                           "Try an older backup (restore_latest picks the newest that verifies).")

    pg_url = _libpq_url(database_url)
    cmd = (["pg_restore", "--no-owner", "--no-acl", "-d", pg_url, str(backup_path)]
           if backup_path.suffix == ".dump" else ["psql", pg_url, "-f", str(backup_path)])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)

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
        raise RuntimeError(f"{cmd[0]} not found — install PostgreSQL client tools") from None


def restore_latest(backup_dir: Path | None, database_url: str, confirm: bool = False) -> RestoreOutcome:
    """Restore the newest backup that verifies, saying which newer ones were passed over and why."""
    if not confirm:
        raise ValueError("Restore requires confirm=True — this will overwrite the current database")
    backup_dir = Path(backup_dir) if backup_dir else _DEFAULT_BACKUP_DIR
    unusable = bc.directory_problem(backup_dir, "backup directory")
    if unusable:
        return RestoreOutcome(
            selection=bc.Selection(chosen=None), notes=[],
            error=f"{unusable}; the database was not touched. Try another directory or a second copy of "
                  "the dumps, or restore a dump by hand with psql / pg_restore.")
    kind = PgDumpKind(backup_dir)
    sel = bc.select_newest_intact(kind)
    out = RestoreOutcome(selection=sel, notes=sel.explain(kind.noun))
    if sel.chosen is None:
        out.error = (f"no database backup in {backup_dir} passes verification; the database was not touched. "
                     + "; ".join(f"{r.id}: {r.reasons[0]}" for r in sel.rejected)
                     + ("" if sel.rejected else "(none found)")
                     + ". Try another directory or a second copy, or restore a dump by hand with "
                       "psql / pg_restore.")
        return out
    out.result = restore_backup(backup_dir / sel.chosen, database_url, confirm=True, verify_first=False)
    return out


@dataclass
class RestoreOutcome:
    selection: bc.Selection
    notes: list[str]
    result: BackupResult | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.result is not None and self.result.success


def verify_backup(backup_path: Path) -> list[str]:
    """Why *backup_path* cannot be trusted; ``[]`` means intact.  No database needed.

    Checks, in order: present and non-empty; the size and sha256 recorded when it was written
    (``.<file>.manifest.json``, absent for dumps made before manifests existed: those are judged
    on structure alone); and the format's own structure (custom: the ``PGDMP`` header;
    plain: pg_dump's closing line, which a truncated dump lacks)."""
    backup_path = Path(backup_path)
    try:
        size = backup_path.stat().st_size
    except OSError:
        return ["file is missing or unreadable"]
    if size == 0:
        return ["file is empty"]
    problems: list[str] = []
    mp = _manifest_path(backup_path)
    if mp.exists():
        try:
            rec = json.loads(mp.read_text("utf-8"))
            if rec.get("size") != size:
                problems.append(f"size is {size}, recorded {rec.get('size')} (truncated or altered)")
            elif rec.get("sha256") != bc.sha256_file(backup_path):
                problems.append("content does not match the checksum recorded when it was written")
        except (OSError, ValueError):
            problems.append("its manifest is unreadable")
    try:
        with open(backup_path, "rb") as f:
            head = f.read(16)
            f.seek(max(0, size - 4096))
            tail = f.read()
    except OSError:
        return [*problems, "file is unreadable"]
    if any(head.startswith(m) or m in head for m in _DRY_RUN_MARKERS):
        return problems
    if backup_path.suffix == ".dump":
        if not head.startswith(b"PGDMP"):
            problems.append("not a pg_dump custom-format archive (header missing)")
        elif shutil.which("pg_restore"):
            try:
                r = subprocess.run(["pg_restore", "--list", str(backup_path)], capture_output=True,
                                   timeout=120, check=False)
                if r.returncode != 0:
                    problems.append("pg_restore cannot read its table of contents")
            except (OSError, subprocess.TimeoutExpired):
                pass
    elif _PLAIN_TRAILER not in tail:
        problems.append("the dump has no closing line (truncated)")
    return problems


class PgDumpKind:
    """:class:`~axiom.infra.backup_core.BackupKind` over a directory of pg_dump files."""

    noun = "database backup"

    def __init__(self, backup_dir: Path) -> None:
        self.dir = Path(backup_dir)

    def candidates(self) -> list[str]:
        return [b.backup_path.name for b in list_backups(self.dir)]

    def check(self, bid: str) -> list[str]:
        return verify_backup(self.dir / bid)


def prune_backups(backup_dir: Path | None = None, retention: bc.RetentionPolicy | None = None, *,
                  apply: bool = False, now: datetime | None = None) -> bc.RetentionPlan:
    """Apply the shared retention policy to dump files.  Never leaves zero verified backups."""
    backup_dir = Path(backup_dir) if backup_dir else _DEFAULT_BACKUP_DIR
    retention = retention or bc.RetentionPolicy(min_free_bytes=MIN_FREE_BYTES)
    with bc.backup_lock(backup_dir / ".lock", busy=lambda: BackupBusy(
            f"another backup or prune is running in {backup_dir}; try again shortly")):
        kind = PgDumpKind(backup_dir)
        infos = []
        for b in list_backups(backup_dir):
            infos.append(bc.BackupInfo(
                id=b.backup_path.name, created_at=datetime.fromisoformat(b.created_at),
                size_bytes=b.size_bytes, verified=not kind.check(b.backup_path.name)))
        plan = bc.plan_retention(infos, retention, now or bc.utcnow())
        if apply:
            for d in plan.delete:
                _discard_partial(backup_dir / d.id)
        return plan


_NAME_STAMP = re.compile(r"^axiom-backup-(\d{8}_\d{6})")


def _order_key(path: Path) -> tuple[str, float, str]:
    """Newest first sorts on this, reversed: the timestamp in the file NAME (a copy to another disk
    resets modification times, a name does not change), then mtime, then the name."""
    m = _NAME_STAMP.match(path.name)
    return (m.group(1) if m else "", path.stat().st_mtime, path.name)


def list_backups(backup_dir: Path | None = None) -> list[BackupResult]:
    """List available backups (both plain .sql and custom .dump), newest first."""
    backup_dir = backup_dir or _DEFAULT_BACKUP_DIR
    if bc.directory_problem(backup_dir):
        return []

    files = [
        f for _flags, ext in _FORMATS.values() for f in backup_dir.glob(f"axiom-backup-*{ext}")
    ]
    backups = []
    for f in sorted(files, key=_order_key, reverse=True):
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
