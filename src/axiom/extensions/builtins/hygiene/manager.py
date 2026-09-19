# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""MoManager — Layer 1 core lifecycle engine.

Provides managed scratch space with:
- acquire/release for files and directories
- Manifest tracking across processes
- Startup sweep for orphan cleanup
- atexit hook for session cleanup
- Periodic sweep for long-running processes
"""

from __future__ import annotations

import atexit
import os
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .manifest import Manifest, ScratchEntry
from .paths import resolve_base_dir


class MoManager:
    """Process-global scratch space manager."""

    def __init__(self, base_dir: Path | None = None, bus: Any | None = None):
        self._base = base_dir or resolve_base_dir()
        self._bus = bus
        self._writable = False
        self._manifest: Manifest | None = None
        self._timer: threading.Timer | None = None
        self._pid = os.getpid()

        # Try to create the base directory
        try:
            self._base.mkdir(parents=True, exist_ok=True)
            self._writable = self._base.is_dir() and os.access(self._base, os.W_OK)
        except OSError:
            self._writable = False

        if self._writable:
            self._manifest = Manifest(self._base / ".tidy-manifest.json")
            self._startup_sweep()
            atexit.register(self._cleanup_session)

    @property
    def base_dir(self) -> Path:
        return self._base

    @property
    def writable(self) -> bool:
        return self._writable

    def set_bus(self, bus: Any) -> None:
        self._bus = bus

    def acquire_file(
        self,
        owner: str,
        suffix: str = "",
        purpose: str = "",
        retention: str = "session",
    ) -> Path | None:
        """Acquire a managed scratch file. Returns None if unavailable."""
        if not self._writable or self._manifest is None:
            return None

        name = uuid4().hex[:8] + suffix
        owner_dir = self._base / owner.replace(".", "/")
        path = owner_dir / name

        try:
            owner_dir.mkdir(parents=True, exist_ok=True)
            path.touch()
        except OSError:
            return None

        entry = ScratchEntry(
            path=str(path),
            owner=owner,
            purpose=purpose,
            retention=retention,
            is_dir=False,
        )
        self._manifest.add(entry)
        self._publish("tidy.acquired", {
            "id": entry.id,
            "path": str(path),
            "owner": owner,
            "retention": retention,
        })
        return path

    def acquire_dir(
        self,
        owner: str,
        purpose: str = "",
        retention: str = "session",
    ) -> Path | None:
        """Acquire a managed scratch directory. Returns None if unavailable."""
        if not self._writable or self._manifest is None:
            return None

        name = uuid4().hex[:8]
        owner_dir = self._base / owner.replace(".", "/")
        path = owner_dir / name

        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None

        entry = ScratchEntry(
            path=str(path),
            owner=owner,
            purpose=purpose,
            retention=retention,
            is_dir=True,
        )
        self._manifest.add(entry)
        self._publish("tidy.acquired", {
            "id": entry.id,
            "path": str(path),
            "owner": owner,
            "retention": retention,
        })
        return path

    def release(self, path: Path | str) -> bool:
        """Release a tracked scratch resource. Returns True if found and deleted."""
        if self._manifest is None:
            return False

        path_str = str(path)
        entry = self._manifest.remove_by_path(path_str)
        if entry is None:
            return False

        self._delete_path(Path(path_str), entry.is_dir)
        self._publish("tidy.released", {
            "id": entry.id,
            "path": path_str,
            "owner": entry.owner,
        })
        return True

    def status(self) -> dict[str, Any]:
        """Return current TIDY status for CLI and health checks."""
        info: dict[str, Any] = {
            "base_dir": str(self._base),
            "writable": self._writable,
            "active_entries": 0,
            "total_size_bytes": 0,
            "entries_by_owner": {},
            "disk_free_bytes": 0,
            "disk_total_bytes": 0,
            "disk_used_pct": 0.0,
        }

        if not self._writable or self._manifest is None:
            return info

        entries = self._manifest.all_entries()
        info["active_entries"] = len(entries)

        total_size = 0
        by_owner: dict[str, int] = {}
        for e in entries:
            size = self._measure_size(Path(e.path), e.is_dir)
            total_size += size
            by_owner[e.owner] = by_owner.get(e.owner, 0) + 1

        info["total_size_bytes"] = total_size
        info["entries_by_owner"] = by_owner

        try:
            usage = shutil.disk_usage(self._base)
            info["disk_free_bytes"] = usage.free
            info["disk_total_bytes"] = usage.total
            info["disk_used_pct"] = round(usage.used / usage.total * 100, 1) if usage.total else 0
        except OSError:
            pass

        return info

    def all_entries(self) -> list[ScratchEntry]:
        """Return all tracked entries (for CLI listing)."""
        if self._manifest is None:
            return []
        return self._manifest.all_entries()

    def sweep(self, project_root: Path | None = None) -> dict[str, Any]:
        """Clean expired entries, orphans, and enforce retention policies.

        Returns counts dict with scratch cleanup results and optional
        retention results.
        """
        if not self._writable or self._manifest is None:
            return {"expired": 0, "orphaned": 0, "errors": 0}

        expired = 0
        orphaned = 0
        errors = 0
        now = datetime.now(UTC)

        for entry in list(self._manifest.all_entries()):
            should_remove = False
            path = Path(entry.path)

            # Check if path still exists
            if not path.exists():
                should_remove = True

            # Check for dead PID
            elif entry.pid != self._pid and not self._pid_alive(entry.pid):
                should_remove = True
                orphaned += 1

            # Check retention expiry
            elif self._is_expired(entry, now):
                should_remove = True
                expired += 1

            if should_remove:
                self._manifest.remove(entry.id)
                if path.exists():
                    if not self._delete_path(path, entry.is_dir):
                        errors += 1

        # Walk base dir for untracked paths (hard-crash orphans)
        tracked_paths = {e.path for e in self._manifest.all_entries()}
        orphaned += self._sweep_untracked(tracked_paths)

        result: dict[str, Any] = {
            "expired": expired,
            "orphaned": orphaned,
            "errors": errors,
        }

        # Retention policy sweep (if project root provided)
        if project_root is not None:
            result["retention"] = self._sweep_retention(project_root)
            # Repo hygiene: only clean pycache (safe), never delete .neut/ items automatically
            result["repo_hygiene"] = self._sweep_repo_hygiene(project_root)

        self._publish("tidy.swept", result)
        return result

    def _sweep_repo_hygiene(self, project_root: Path) -> dict[str, Any]:
        """Run lightweight repo hygiene as part of sweep."""
        try:
            from .repo_hygiene import clean_clutter
            return clean_clutter(project_root, dry_run=False)
        except Exception:
            return {"skipped": True}

    def _sweep_retention(self, project_root: Path) -> dict[str, Any]:
        """Run retention policy enforcement as part of sweep."""
        try:
            from .retention import (
                execute_retention,
                load_retention_config,
                scan_retention,
            )
        except ImportError:
            return {"skipped": True, "reason": "pyyaml not installed"}

        config_dir = project_root / "runtime" / "config"
        example_dir = project_root / "runtime" / "config.example"
        policies, legal_hold, audit_path = load_retention_config(
            config_dir, example_dir,
        )
        if not policies:
            return {"skipped": True, "reason": "no retention config"}

        actions = scan_retention(project_root, policies, legal_hold)
        if not actions:
            return {"deleted": 0, "skipped": 0, "bytes_freed": 0, "errors": 0}

        return execute_retention(
            actions, project_root / audit_path,
        )

    def purge(self) -> dict[str, int]:
        """Delete all tracked entries and wipe the base directory contents."""
        if not self._writable or self._manifest is None:
            return {"deleted": 0}

        count = 0
        for entry in list(self._manifest.all_entries()):
            self._manifest.remove(entry.id)
            path = Path(entry.path)
            if path.exists():
                self._delete_path(path, entry.is_dir)
                count += 1

        # Also clean any untracked files in base
        for child in self._base.iterdir():
            if child.name == ".tidy-manifest.json":
                continue
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
                count += 1
            except OSError:
                pass

        return {"deleted": count}

    def start_periodic(self, interval: float = 300) -> threading.Timer | None:
        """Start periodic sweep for long-running processes. Returns timer."""
        if not self._writable:
            return None

        def _tick():
            self.sweep()
            self._timer = threading.Timer(interval, _tick)
            self._timer.daemon = True
            self._timer.start()

        self._timer = threading.Timer(interval, _tick)
        self._timer.daemon = True
        self._timer.start()
        return self._timer

    def stop_periodic(self) -> None:
        """Cancel the periodic sweep timer."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    # --- Internal ---

    def _startup_sweep(self) -> None:
        """Run on init: clean up entries from dead processes and expired retention."""
        self.sweep()

    def _cleanup_session(self) -> None:
        """atexit hook: release all entries owned by this PID with session/transient retention."""
        if self._manifest is None:
            return
        for entry in list(self._manifest.all_entries()):
            if entry.pid == self._pid and entry.retention in ("session", "transient"):
                self._manifest.remove(entry.id)
                path = Path(entry.path)
                if path.exists():
                    try:
                        self._delete_path(path, entry.is_dir)
                    except Exception:
                        pass

    def _is_expired(self, entry: ScratchEntry, now: datetime) -> bool:
        """Check if an entry's retention period has expired."""
        if entry.retention in ("session", "transient"):
            # transient entries older than 5 minutes without explicit release
            if entry.retention == "transient":
                try:
                    created = datetime.fromisoformat(entry.created_at)
                    age_seconds = (now - created).total_seconds()
                    return age_seconds > 300  # 5 minutes
                except (ValueError, TypeError):
                    return False
            return False

        try:
            created = datetime.fromisoformat(entry.created_at)
            age_seconds = (now - created).total_seconds()
        except (ValueError, TypeError):
            return False

        if entry.retention == "hour":
            return age_seconds > 3600
        if entry.retention == "day":
            return age_seconds > 86400
        return False

    def _sweep_untracked(self, tracked_paths: set[str]) -> int:
        """Walk base dir for paths not in the manifest."""
        orphaned = 0
        try:
            for child in self._base.iterdir():
                if child.name == ".tidy-manifest.json":
                    continue
                if child.is_dir():
                    # Walk one level deeper (owner dirs contain actual entries)
                    for sub in child.iterdir():
                        if str(sub) not in tracked_paths:
                            self._delete_path(sub, sub.is_dir())
                            orphaned += 1
                elif str(child) not in tracked_paths:
                    child.unlink(missing_ok=True)
                    orphaned += 1
        except OSError:
            pass
        # Clean empty owner dirs
        try:
            for child in self._base.iterdir():
                if child.is_dir() and child.name != ".tidy-manifest.json":
                    try:
                        if not any(child.iterdir()):
                            child.rmdir()
                    except OSError:
                        pass
        except OSError:
            pass
        return orphaned

    def _delete_path(self, path: Path, is_dir: bool) -> bool:
        """Delete a file or directory. Returns True on success."""
        try:
            if is_dir or path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    def _measure_size(self, path: Path, is_dir: bool) -> int:
        """Measure size of a file or directory in bytes."""
        try:
            if is_dir or path.is_dir():
                total = 0
                for dirpath, _dirnames, filenames in os.walk(path):
                    for f in filenames:
                        try:
                            total += os.path.getsize(os.path.join(dirpath, f))
                        except OSError:
                            pass
                return total
            return path.stat().st_size if path.exists() else 0
        except OSError:
            return 0

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Check if a process is still alive."""
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False

    def _publish(self, topic: str, data: dict[str, Any]) -> None:
        """Publish an event if bus is wired."""
        if self._bus is not None:
            try:
                self._bus.publish(topic, data, source="tidy")
            except Exception:
                pass
