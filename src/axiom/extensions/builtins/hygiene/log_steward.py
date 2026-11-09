# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TIDY Log Steward — log lifecycle management.

TIDY owns log rotation, size monitoring, promoted-file cleanup, and
audit table size reporting. Runs on schedule without user involvement.

Responsibilities:
  - System log rotation: rotate runtime/logs/system.log at 50 MB, keep 7 days
  - JSONL size check: warn if any audit .jsonl file exceeds 100 MB
  - Promoted file cleanup: delete *.jsonl.promoted.* files older than 30 days
  - Audit table size report: surface row counts via backend_info()
  - Emit signals for oversized logs (logs.size_warning)
"""

from __future__ import annotations

import dataclasses
import logging
import os
import time
from pathlib import Path

from axiom import REPO_ROOT
from axiom.infra.audit_log import AuditLog
from axiom.infra.axiom_logging import axiom_signal

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SYSTEM_LOG_NAME = "system.log"
_ROTATE_MAX_BYTES = 50 * 1024 * 1024   # 50 MB
_ROTATE_KEEP = 7                         # numbered backups: .1 … .7
_JSONL_WARN_BYTES = 100 * 1024 * 1024   # 100 MB
_PROMOTED_MAX_AGE_DAYS = 30

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LogSteward
# ---------------------------------------------------------------------------

class LogSteward:
    """Performs log lifecycle maintenance on behalf of the TIDY agent.

    Parameters
    ----------
    log_dir:
        Directory that contains ``system.log`` and the ``audit/``
        sub-directory.  Defaults to ``REPO_ROOT/runtime/logs``.
    """

    def __init__(self, log_dir: Path | None = None) -> None:
        self._log_dir: Path = Path(log_dir) if log_dir is not None else REPO_ROOT / "runtime" / "logs"
        self._audit_dir: Path = self._log_dir / "audit"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_sweep(self) -> dict:
        """Run all log-steward checks.

        Returns
        -------
        dict with keys:
            ``rotated`` (bool) — whether system.log was rotated this sweep.
            ``oversized_files`` (list[str]) — audit JSONL paths exceeding 100 MB.
            ``cleaned_promoted`` (int) — count of promoted files deleted.
            ``signals_emitted`` (int) — total ``logs.size_warning`` signals fired.
            ``audit_backend`` (dict) — result of ``_report_audit_table_sizes()``.
        """
        rotated = self._rotate_system_logs()
        oversized = self._check_jsonl_sizes()
        cleaned = self._cleanup_promoted_files()
        audit_info = self._report_audit_table_sizes()

        summary = {
            "rotated": rotated,
            "oversized_files": [str(p) for p in oversized],
            "cleaned_promoted": cleaned,
            "signals_emitted": len(oversized),
            "audit_backend": audit_info,
        }
        logger.info(
            "LogSteward sweep complete",
            extra={
                "rotated": rotated,
                "oversized_count": len(oversized),
                "cleaned_promoted": cleaned,
            },
        )
        return summary

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _rotate_system_logs(self) -> bool:
        """Rotate ``system.log`` if it exceeds 50 MB.

        Implements a manual rotation that mirrors
        ``logging.handlers.RotatingFileHandler`` semantics:
        existing ``.1``–``.7`` backups are shifted up by one; the oldest
        (``.7``) is dropped; ``system.log`` is renamed to ``system.log.1``.

        File locking is applied around the rename sequence using the same
        ``fcntl.flock`` advisory-lock convention as ``locked_append_jsonl``
        in ``axiom.infra.state``.

        Returns
        -------
        bool
            ``True`` if rotation was performed, ``False`` otherwise.
        """
        log_path = self._log_dir / _SYSTEM_LOG_NAME
        if not log_path.exists():
            return False

        try:
            size = log_path.stat().st_size
        except OSError:
            return False

        if size < _ROTATE_MAX_BYTES:
            return False

        # Acquire an exclusive advisory lock on a dedicated lock file so that
        # no concurrent writer renames the file at the same time.
        lock_path = self._log_dir / ".system_log.rotate.lock"
        lock_fd = None
        try:
            lock_fd = open(lock_path, "w")  # noqa: WPS515
            try:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except ImportError:
                # Windows: advisory locking not available; proceed without it.
                pass

            # Re-check size under the lock — another process may have already
            # rotated while we were waiting.
            try:
                size = log_path.stat().st_size
            except OSError:
                return False
            if size < _ROTATE_MAX_BYTES:
                return False

            # Shift existing backups: .6→.7, .5→.6, …, .1→.2, then rename active→.1
            for n in range(_ROTATE_KEEP - 1, 0, -1):
                src = self._log_dir / f"{_SYSTEM_LOG_NAME}.{n}"
                dst = self._log_dir / f"{_SYSTEM_LOG_NAME}.{n + 1}"
                if src.exists():
                    try:
                        src.rename(dst)
                    except OSError as exc:
                        logger.warning("log rotation rename failed: %s → %s: %s", src, dst, exc)

            # Move active log to .1
            backup_1 = self._log_dir / f"{_SYSTEM_LOG_NAME}.1"
            try:
                log_path.rename(backup_1)
            except OSError as exc:
                logger.error("log rotation failed renaming system.log: %s", exc)
                return False

        finally:
            if lock_fd is not None:
                try:
                    import fcntl
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                lock_fd.close()

        logger.info(
            "system.log rotated",
            extra={"rotated_size_bytes": size, "backup": str(backup_1)},
        )
        return True

    def _check_jsonl_sizes(self) -> list[Path]:
        """Return audit JSONL files exceeding 100 MB and emit size-warning signals.

        Scans ``<log_dir>/audit/*.jsonl``.  For each file over the threshold,
        a ``logs.size_warning`` signal is emitted via ``axiom_signal``.

        Returns
        -------
        list[Path]
            Paths of all oversized JSONL files found.
        """
        oversized: list[Path] = []

        if not self._audit_dir.is_dir():
            return oversized

        for entry in sorted(self._audit_dir.iterdir()):
            if entry.suffix != ".jsonl" or not entry.is_file():
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            if size >= _JSONL_WARN_BYTES:
                oversized.append(entry)
                logger.warning(
                    "audit JSONL exceeds size threshold",
                    extra=axiom_signal(
                        "logs.size_warning",
                        file=str(entry),
                        size_bytes=size,
                        threshold_bytes=_JSONL_WARN_BYTES,
                    ),
                )

        return oversized

    def _cleanup_promoted_files(self) -> int:
        """Delete ``*.jsonl.promoted.*`` files older than 30 days.

        Scans the entire ``<log_dir>`` tree (recursively) for promoted-file
        artefacts left behind after a JSONL file was promoted to a durable
        store.

        Returns
        -------
        int
            Number of files deleted.
        """
        cutoff_ts = time.time() - _PROMOTED_MAX_AGE_DAYS * 86_400
        deleted = 0

        for dirpath, _dirnames, filenames in os.walk(self._log_dir):
            for filename in filenames:
                # Match pattern: <stem>.jsonl.promoted.<anything>
                if ".jsonl.promoted." not in filename:
                    continue
                full_path = Path(dirpath) / filename
                try:
                    mtime = full_path.stat().st_mtime
                except OSError:
                    continue
                if mtime < cutoff_ts:
                    try:
                        full_path.unlink()
                        deleted += 1
                        logger.debug("deleted promoted file: %s", full_path)
                    except OSError as exc:
                        logger.warning("could not delete promoted file %s: %s", full_path, exc)

        return deleted

    def _report_audit_table_sizes(self) -> dict:
        """Return audit backend metadata as a plain dict.

        Calls ``AuditLog.get().backend_info()`` and converts the resulting
        ``BackendInfo`` dataclass to a dictionary via ``dataclasses.asdict``.

        Returns
        -------
        dict
            Keys depend on the active backend (e.g. ``backend``,
            ``log_size_bytes`` for the JSONL backend; ``backend``,
            ``row_count`` for a SQL backend).
        """
        try:
            info = AuditLog.get().backend_info()
            return dataclasses.asdict(info)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not retrieve audit backend info: %s", exc)
            return {"error": str(exc)}
