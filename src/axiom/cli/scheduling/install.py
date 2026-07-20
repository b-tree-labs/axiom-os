# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`install_schedules` — multi-schedule orchestrator (issue #205, slice 3).

Brings Backend + Runner + artifact discovery together. For each artifact
under `<project_root>/deploy/<host>/`, asks the backend to install it
on the host via the runner. Per-schedule outcomes get rolled into an
`InstallReport`; one schedule's failure does NOT block the others.

The orchestrator stays backend-agnostic by calling
`backend.install_artifact(runner, schedule_name, artifact_content)`.
Each backend parses its own artifact format — cron parses cron files,
launchd parses plists, etc.
"""

from __future__ import annotations

from pathlib import Path

from .protocols import (
    InstallOutcome,
    InstallReport,
    RemoteRunner,
    SchedulerBackend,
)


def _deploy_dir(project_root: Path, host: str) -> Path:
    return project_root / "deploy" / host


def _artifact_extension(backend: SchedulerBackend) -> str:
    """Pull the file extension out of `backend.artifact_filename`.

    Avoids requiring backends to expose an extension property directly —
    they only have to know how to name an artifact for a given schedule.
    """
    name = backend.artifact_filename("__probe__")
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[1]


def _discover_schedules(
    project_root: Path,
    host: str,
    backend: SchedulerBackend,
) -> list[Path]:
    """List every `<host>/<name>.<ext>` artifact for this backend.
    Returns empty list when the deploy dir doesn't exist."""
    host_dir = _deploy_dir(project_root, host)
    if not host_dir.is_dir():
        return []
    ext = _artifact_extension(backend)
    pattern = f"*.{ext}" if ext else "*"
    return sorted(host_dir.glob(pattern))


def install_schedules(
    *,
    project_root: Path,
    host: str,
    runner: RemoteRunner,
    backend: SchedulerBackend,
    name_filter: str | None = None,
    dry_run: bool = False,
) -> InstallReport:
    """Install every (or filtered) schedule artifact for `host` via `backend`.

    Per-schedule failures are caught + surfaced in the report; one
    failure does NOT abort the rest. The caller decides how to react to
    `report.all_ok is False` (CLI exits non-zero; programmatic callers
    can keep going).

    Dry-run mode discovers schedules and reports them as `skipped`
    without invoking `backend.install_artifact` — useful for "what
    would I install" smoke before touching the host.
    """
    outcomes: list[InstallOutcome] = []
    for artifact_path in _discover_schedules(project_root, host, backend):
        schedule_name = artifact_path.stem
        if name_filter is not None and schedule_name != name_filter:
            continue
        if dry_run:
            outcomes.append(
                InstallOutcome(schedule_name=schedule_name, status="skipped")
            )
            continue
        try:
            content = artifact_path.read_text()
        except OSError as exc:
            outcomes.append(
                InstallOutcome(
                    schedule_name=schedule_name,
                    status="failed",
                    error=f"read artifact: {exc}",
                )
            )
            continue
        try:
            backend.install_artifact(
                runner=runner,
                schedule_name=schedule_name,
                artifact_content=content,
            )
        except Exception as exc:  # noqa: BLE001 — surface as per-schedule error
            outcomes.append(
                InstallOutcome(
                    schedule_name=schedule_name,
                    status="failed",
                    error=str(exc),
                )
            )
            continue
        outcomes.append(
            InstallOutcome(schedule_name=schedule_name, status="installed")
        )

    return InstallReport(
        host=host,
        backend=backend.name,
        outcomes=outcomes,
    )
