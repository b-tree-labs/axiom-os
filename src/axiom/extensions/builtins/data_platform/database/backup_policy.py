# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""BackupPolicy — the persisted database-backup posture for one install.

A production node's backup story is configuration, not folklore. This
module owns the TOML-persisted policy the ``data.backup`` /
``data.backup_validate`` skills and the orchestrator's scheduled dispatch
all read (same persistence model as :mod:`..agents.plinth.connectors`).

TOML on-disk shape (default ``$AXI_STATE/plinth/backup_policy.toml``)::

    [backup_policy]
    enabled = true
    schedule = "0 2 * * *"           # PULSE cadence string (cron/@shortcut/ISO-8601)
    validate_schedule = "30 6 * * *"
    target_root = "/natura/axiom-data/backups"
    retention_count = 14
    schemas = ["rag", "memory"]      # omitted → whole database
    offbox = "box"                   # "none" | "box"
    box_folder_id = "123456789"

Schedule strings are validated through :func:`schedule.formats.parse`
(PULSE's cadence codec) so anything stored here is guaranteed
registrable as a cadence.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from axiom.infra.paths import get_user_state_dir

_OFFBOX_KINDS = ("none", "box")


@dataclass(frozen=True)
class BackupPolicy:
    """Persisted backup posture. Defaults are safe-but-disabled: nothing
    fires until an operator flips ``enabled`` (which is what arms the
    orchestrator's default cadences)."""

    enabled: bool = False
    schedule: str = "0 2 * * *"
    """When ``data.backup`` fires (PULSE cadence string; default 02:00 UTC)."""
    validate_schedule: str = "30 6 * * *"
    """When ``data.backup_validate`` fires (default 06:30 UTC — after the dump)."""
    target_root: str = "~/.axi/backups"
    """Backup artifact directory. Ops point this at durable storage
    (kept deliberately free of any deployment-specific default)."""
    retention_count: int = 14
    """Keep the newest N artifacts; older ones are pruned after each backup."""
    schemas: list[str] | None = None
    """Schema scoping for pg_dump (``-n`` flags); ``None`` = whole database."""
    offbox: str = "none"
    """Off-box replication leg: ``"none"`` or ``"box"`` (Box upload)."""
    box_folder_id: str | None = None
    """Destination Box folder when ``offbox == "box"``."""

    def resolved_target_root(self) -> Path:
        """``target_root`` with ``~`` expanded."""
        return Path(self.target_root).expanduser()


def cadence_for(schedule: str):
    """Parse a policy schedule string into a PULSE :class:`Cadence`.

    Delegates to the schedule extension's format codec (cron 5/6-field,
    ``@daily``-style shortcuts, ISO-8601 durations, RRULE). Raises
    ``FormatError`` on anything unparseable — the same error surface
    :func:`validate_policy` reports.
    """
    from axiom.extensions.builtins.schedule.formats import parse

    return parse(schedule)


def validate_policy(policy: BackupPolicy) -> list[str]:
    """Return fail-closed validation errors (empty list = valid)."""
    errors: list[str] = []
    if policy.retention_count < 1:
        errors.append(
            f"retention_count must be >= 1 (got {policy.retention_count}); "
            "keeping zero backups is not a backup posture"
        )
    if policy.offbox not in _OFFBOX_KINDS:
        errors.append(f"offbox must be one of {_OFFBOX_KINDS} (got {policy.offbox!r})")
    if policy.offbox == "box" and not policy.box_folder_id:
        errors.append("offbox='box' requires box_folder_id")
    if not policy.target_root:
        errors.append("target_root must be a non-empty path")
    for field_name, value in (
        ("schedule", policy.schedule),
        ("validate_schedule", policy.validate_schedule),
    ):
        try:
            cadence_for(value)
        except Exception as exc:  # FormatError et al. — report, don't raise
            errors.append(f"{field_name}: not a PULSE cadence string ({exc})")
    return errors


def backup_policy_path(*, state_dir: Path | None = None) -> Path:
    """Where the policy TOML lives."""
    base = state_dir or get_user_state_dir()
    return base / "plinth" / "backup_policy.toml"


def save_backup_policy(policy: BackupPolicy, *, state_dir: Path | None = None) -> Path:
    """Persist the policy to TOML; returns the written path."""
    path = backup_policy_path(state_dir=state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = ["[backup_policy]"]
    lines.append(f"enabled = {'true' if policy.enabled else 'false'}")
    lines.append(f"schedule = {_toml_str(policy.schedule)}")
    lines.append(f"validate_schedule = {_toml_str(policy.validate_schedule)}")
    lines.append(f"target_root = {_toml_str(policy.target_root)}")
    lines.append(f"retention_count = {policy.retention_count}")
    if policy.schemas is not None:
        rendered = ", ".join(_toml_str(s) for s in policy.schemas)
        lines.append(f"schemas = [{rendered}]")
    lines.append(f"offbox = {_toml_str(policy.offbox)}")
    if policy.box_folder_id is not None:
        lines.append(f"box_folder_id = {_toml_str(policy.box_folder_id)}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def load_backup_policy(*, state_dir: Path | None = None) -> BackupPolicy | None:
    """Load the persisted policy, or ``None`` when no policy file exists."""
    path = backup_policy_path(state_dir=state_dir)
    if not path.exists():
        return None
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    blob = data.get("backup_policy") or {}
    schemas = blob.get("schemas")
    return BackupPolicy(
        enabled=bool(blob.get("enabled", False)),
        schedule=str(blob.get("schedule", BackupPolicy.schedule)),
        validate_schedule=str(blob.get("validate_schedule", BackupPolicy.validate_schedule)),
        target_root=str(blob.get("target_root", BackupPolicy.target_root)),
        retention_count=int(blob.get("retention_count", BackupPolicy.retention_count)),
        schemas=[str(s) for s in schemas] if schemas is not None else None,
        offbox=str(blob.get("offbox", BackupPolicy.offbox)),
        box_folder_id=(str(blob["box_folder_id"]) if blob.get("box_folder_id") else None),
    )


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


__all__ = [
    "BackupPolicy",
    "backup_policy_path",
    "cadence_for",
    "load_backup_policy",
    "save_backup_policy",
    "validate_policy",
]
