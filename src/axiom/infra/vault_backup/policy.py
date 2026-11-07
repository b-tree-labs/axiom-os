# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Vault-backup policy: where, how often, how long to keep."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom.infra.backup_core import RetentionPolicy

#: Vault retention differs from the snapshot default on purpose.  Archives are
#: tiny, so there is no storage argument for thinning early; but every archive
#: is a copy of live credentials, so there IS a risk argument for not keeping
#: dense history long.  Hence: a short high-frequency window (6h, not 24h), a
#: modest "newest N" (10, not 24), the same dailies/weeklies, and monthlies
#: kept much longer (24, not 6) because "the credential I had a year ago" is
#: the realistic reason to open an old archive.
VAULT_RETENTION = RetentionPolicy(
    keep_last=10,
    keep_recent_hours=6,
    keep_daily=14,
    keep_weekly=8,
    keep_monthly=24,
    protect_newest=3,
    max_count=60,
    max_bytes=256 * 1024**2,
    min_free_bytes=64 * 1024**2,
    min_free_fraction=0.02,
)

_PREFIX = "vault.backup."


@dataclass(frozen=True)
class BackupPolicy:
    enabled: bool = True
    interval_hours: float = 24.0
    debounce_seconds: float = 300.0
    auto_prune: bool = True
    second_copy: str = ""
    extra_files: tuple[str, ...] = ()
    retention: RetentionPolicy = field(default_factory=lambda: VAULT_RETENTION)


def policy_from_settings(settings: dict[str, Any] | None = None) -> BackupPolicy:
    s = dict(settings or {})
    base = BackupPolicy()
    r = base.retention

    def get(name: str, default: Any, cast) -> Any:
        try:
            return cast(s.get(_PREFIX + name, default))
        except (TypeError, ValueError):
            return default

    def as_bool(v: Any) -> bool:
        return v.strip().lower() in {"1", "true", "yes", "on"} if isinstance(v, str) else bool(v)

    retention = RetentionPolicy(
        keep_last=get("keep_last", r.keep_last, int),
        keep_recent_hours=get("keep_recent_hours", r.keep_recent_hours, int),
        keep_daily=get("keep_daily", r.keep_daily, int),
        keep_weekly=get("keep_weekly", r.keep_weekly, int),
        keep_monthly=get("keep_monthly", r.keep_monthly, int),
        protect_newest=get("protect_newest", r.protect_newest, int),
        max_count=get("max_count", r.max_count, int),
        max_bytes=get("max_bytes", r.max_bytes, int),
        min_free_bytes=get("min_free_bytes", r.min_free_bytes, int),
        min_free_fraction=get("min_free_fraction", r.min_free_fraction, float),
    ).normalized()
    return BackupPolicy(
        enabled=get("enabled", base.enabled, as_bool),
        interval_hours=get("interval_hours", base.interval_hours, float),
        debounce_seconds=get("debounce_seconds", base.debounce_seconds, float),
        auto_prune=get("auto_prune", base.auto_prune, as_bool),
        second_copy=get("second_copy", base.second_copy, lambda v: str(v or "")),
        extra_files=get("extra_files", (), lambda v: tuple(str(x) for x in (v or ()))),
        retention=retention,
    )


def load_policy(state_dir: Path) -> BackupPolicy:
    """Read ``[vault.backup]`` from ``<state>/settings.toml``."""
    flat: dict[str, Any] = {}
    try:
        import tomllib

        data = tomllib.loads((Path(state_dir) / "settings.toml").read_text(encoding="utf-8"))
        table = data.get("vault", {}).get("backup", {})
        if isinstance(table, dict):
            flat = {_PREFIX + k: v for k, v in table.items()}
        for k, v in data.items():
            if isinstance(k, str) and k.startswith(_PREFIX):
                flat[k] = v
    except (OSError, ValueError):
        pass
    return policy_from_settings(flat)


# -- locations ---------------------------------------------------------------


def default_root(state_dir: Path) -> Path:
    """``<state>/vault-backups``.  Excluded from state snapshots by rule."""
    return Path(state_dir) / "vault-backups"


def secrets_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "secrets"


def recipient_path(state_dir: Path) -> Path:
    return secrets_dir(state_dir) / "vault-backup-recipient.json"


def head_path(state_dir: Path) -> Path:
    return secrets_dir(state_dir) / "vault-backup-head.json"


def pending_path(state_dir: Path) -> Path:
    return secrets_dir(state_dir) / "vault-backup-pending.json"
