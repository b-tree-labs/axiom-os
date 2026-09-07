# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`axi schedule` — per-host cron primitives (issue #203).

Background: agents that need recurring host-side work (heartbeats,
doctor sweeps, hygiene runs) historically dumped host-coupled scripts
into `scripts/` with hardcoded `$HOME/Projects/...` paths — a pattern
captured by the `check_scripts_with_hardcoded_paths` signal in
`hygiene.git_signals` (slice 6).

This module owns the *render* side: `axi schedule create` writes a
portable cron artifact to `deploy/<host>/<name>.cron` with `${REPO_DIR}`
placeholders that are substituted at install time. The *apply* side
(`axi schedule install --host <host>`) lands in slice 9.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from axiom.cli.scheduling import install_schedules

_BAD_PATH_CHARS = ("/", "\\", "\x00")


def _validate_segment(value: str, *, field: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string, got {type(value).__name__}")
    if not value:
        raise ValueError(f"{field} must not be empty")
    if value in (".", ".."):
        raise ValueError(f"{field} must not be a relative segment, got {value!r}")
    if any(ch in value for ch in _BAD_PATH_CHARS):
        raise ValueError(
            f"{field} must not contain path separators or null bytes, got {value!r}"
        )


def _validate_cron(expr: str) -> None:
    if not isinstance(expr, str) or not expr.strip():
        raise ValueError("cron expression must not be empty")
    # Standard crontab is 5 whitespace-separated fields. Macros like
    # `@hourly` / `@daily` are a different syntax — accept those too.
    stripped = expr.strip()
    if stripped.startswith("@"):
        # Common macros: @reboot, @hourly, @daily, @weekly, @monthly, @yearly,
        # @annually, @midnight. We don't enumerate exhaustively — just refuse
        # `@` followed by whitespace or nothing.
        if len(stripped) == 1 or stripped[1].isspace():
            raise ValueError(f"cron macro must follow `@` with a keyword, got {expr!r}")
        return
    fields = stripped.split()
    if len(fields) != 5:
        raise ValueError(
            f"cron expression must have 5 fields (minute hour day month weekday) "
            f"or a `@macro`, got {len(fields)} field(s): {expr!r}"
        )


def _deploy_dir(project_root: Path, host: str) -> Path:
    return project_root / "deploy" / host


def create_schedule(
    *,
    project_root: Path,
    name: str,
    host: str,
    cron: str,
    command: str,
    backend_name: str = "cron",
) -> Path:
    """Render a host-scoped schedule artifact to ``deploy/<host>/<name>.<ext>``.

    Delegates render to the named backend (`cron` by default) so the
    artifact format matches what `axi schedule install --backend <name>`
    expects. Previously this verb hand-rolled cron output without the
    `# axi schedule managed:` marker that `CronBackend.install_artifact`
    requires — a quiet divergence that surfaced only at install time.

    Re-running with the same `(name, host, backend_name)` overwrites
    the artifact — the caller is updating, not appending.
    """
    _validate_segment(name, field="name")
    _validate_segment(host, field="host")
    _validate_cron(cron)
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must not be empty")

    backend = _backend_for(backend_name)
    artifact = backend.render(schedule_name=name, cron=cron, command=command)

    out_dir = _deploy_dir(project_root, host)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / backend.artifact_filename(name)
    out.write_text(artifact)
    return out


_KNOWN_BACKEND_EXTENSIONS: tuple[str, ...] = (
    "cron", "plist", "systemd", "ps1",
)


def list_schedules(project_root: Path) -> list[dict]:
    """Return every schedule artifact under ``deploy/`` as a dict with
    name + host + backend + path. Returns ``[]`` when ``deploy/`` doesn't
    exist. Backend is inferred from the file extension."""
    ext_to_backend = {
        "cron": "cron", "plist": "launchd", "systemd": "systemd", "ps1": "wintasks",
    }
    deploy = project_root / "deploy"
    if not deploy.is_dir():
        return []
    out: list[dict] = []
    for host_dir in sorted(deploy.iterdir()):
        if not host_dir.is_dir():
            continue
        for ext in _KNOWN_BACKEND_EXTENSIONS:
            for artifact in sorted(host_dir.glob(f"*.{ext}")):
                out.append(
                    {
                        "name": artifact.stem,
                        "host": host_dir.name,
                        "backend": ext_to_backend[ext],
                        "path": str(artifact.relative_to(project_root)),
                    }
                )
    return out


# ---------------------------------------------------------------------------
# CLI entry point (registered in axiom_cli.SUBCOMMANDS)
# ---------------------------------------------------------------------------


def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="axi schedule",
        description="Render and manage per-host cron primitives.",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    p_create = subparsers.add_parser(
        "create",
        help="Render a host-scoped cron artifact to deploy/<host>/<name>.cron",
    )
    p_create.add_argument("name", help="Schedule name (filesystem-safe)")
    p_create.add_argument("--host", required=True, help="Target host identifier")
    p_create.add_argument(
        "--cron", required=True,
        help="Cron expression (5 fields or @macro)",
    )
    p_create.add_argument(
        "--command", required=True,
        help="Command to schedule; use ${REPO_DIR} / ${PROJECT_ROOT} placeholders",
    )
    p_create.add_argument(
        "--backend", default="cron",
        choices=["cron", "launchd", "systemd", "wintasks"],
        help="Backend the schedule will be installed under (default: cron). "
             "Must match the --backend used at install time, or rely on "
             "auto-detect picking the same one.",
    )

    subparsers.add_parser("list", help="List all schedule artifacts under deploy/")

    p_install = subparsers.add_parser(
        "install",
        help="Apply schedule artifacts on a remote host (issue #205)",
    )
    p_install.add_argument("--host", required=True, help="Target host (ssh-reachable)")
    p_install.add_argument(
        "--backend", default="auto",
        choices=["auto", "cron", "launchd", "systemd", "wintasks"],
        help="SchedulerBackend impl (default: cron; launchd / systemd-timer follow-up)",
    )
    p_install.add_argument(
        "--name", default=None,
        help="Filter to a single schedule by name",
    )
    p_install.add_argument(
        "--dry-run", action="store_true",
        help="Discover schedules and report without applying",
    )

    p_uninstall = subparsers.add_parser(
        "uninstall",
        help="Remove a schedule from a remote host",
    )
    p_uninstall.add_argument("--host", required=True)
    p_uninstall.add_argument(
        "--name", required=True,
        help="Schedule name to uninstall",
    )
    p_uninstall.add_argument(
        "--backend", default="auto",
        choices=["auto", "cron", "launchd", "systemd", "wintasks"],
    )

    parsed = parser.parse_args(args)

    from axiom.infra.paths import get_project_root

    project_root = get_project_root()

    if parsed.action == "create":
        try:
            out = create_schedule(
                project_root=project_root,
                name=parsed.name,
                host=parsed.host,
                cron=parsed.cron,
                command=parsed.command,
                backend_name=parsed.backend,
            )
        except ValueError as exc:
            parser.exit(2, f"axi schedule create: {exc}\n")
        print(f"Wrote {out.relative_to(project_root)}")
        return 0
    if parsed.action == "list":
        rows = list_schedules(project_root)
        if not rows:
            print("(no schedule artifacts)")
            return 0
        for row in rows:
            print(
                f"  {row['host']}/{row['name']}  [{row['backend']}]"
                f"  →  {row['path']}"
            )
        return 0
    if parsed.action == "install":
        runner = _ssh_runner_factory(host=parsed.host)
        backend_name = _resolve_backend_name(
            requested=parsed.backend, runner=runner, dry_run=parsed.dry_run,
        )
        backend = _backend_for(backend_name)
        report = install_schedules(
            project_root=project_root,
            host=parsed.host,
            runner=runner,
            backend=backend,
            name_filter=parsed.name,
            dry_run=parsed.dry_run,
        )
        if not report.outcomes:
            # Empty outcome is silent today — bad UX when artifacts exist
            # in a different backend's format. Surface the mismatch.
            mismatched = _other_backend_artifacts(
                project_root, parsed.host, backend_name,
            )
            if mismatched:
                msg_lines = [
                    f"axi schedule install: no {backend_name!r} artifacts "
                    f"for host {parsed.host!r}, but found schedules in "
                    f"other backend formats:",
                ]
                for row in mismatched:
                    msg_lines.append(
                        f"  - {row['name']}  [{row['backend']}]  →  {row['path']}"
                    )
                msg_lines.append(
                    f"Hint: re-run with --backend {mismatched[0]['backend']!r}, "
                    f"or recreate the schedule with `axi schedule create "
                    f"... --backend {backend_name}`."
                )
                print("\n".join(msg_lines))
                return 1
            print(f"(no schedule artifacts for host {parsed.host!r})")
            return 0
        print(
            f"{report.backend} → {report.host}: "
            f"{len(report.outcomes)} schedule(s)"
        )
        for outcome in report.outcomes:
            tag = outcome.status.upper()
            line = f"  [{tag:9s}] {outcome.schedule_name}"
            if outcome.error:
                line += f"  — {outcome.error}"
            print(line)
        return 0 if report.all_ok else 1
    if parsed.action == "uninstall":
        runner = _ssh_runner_factory(host=parsed.host)
        backend_name = _resolve_backend_name(
            requested=parsed.backend, runner=runner, dry_run=False,
        )
        backend = _backend_for(backend_name)

        # Existence check: if the user wrote no artifact for this schedule
        # in this backend, refuse rather than running a misleading
        # "uninstalled" for something that was never installed by us.
        artifact_path = (
            _deploy_dir(project_root, parsed.host)
            / backend.artifact_filename(parsed.name)
        )
        if not artifact_path.exists():
            other = [
                row for row in list_schedules(project_root)
                if row["host"] == parsed.host and row["name"] == parsed.name
            ]
            if other:
                hint = (
                    f" — found {parsed.name!r} under "
                    f"--backend {other[0]['backend']!r}; "
                    f"re-run with that backend"
                )
            else:
                hint = (
                    f" — no schedule named {parsed.name!r} for host "
                    f"{parsed.host!r} in this project"
                )
            print(f"axi schedule uninstall: nothing to remove{hint}")
            return 1

        try:
            backend.uninstall(runner=runner, schedule_name=parsed.name)
        except Exception as exc:  # noqa: BLE001
            print(f"uninstall failed: {exc}")
            return 1
        print(f"{report_backend_name(backend)} → {parsed.host}: uninstalled {parsed.name}")
        return 0
    return 1


# ---------------------------------------------------------------------------
# Test-injectable hooks (private)
# ---------------------------------------------------------------------------


def _ssh_runner_factory(*, host: str):
    """Build the default RemoteRunner for ``host``. ``localhost`` /
    ``local`` route to `LocalRunner` (no ssh round-trip to self);
    anything else routes to `SSHRunner`. Tests monkey-patch this hook
    to inject a `FakeRunner` without touching subprocess."""
    from axiom.cli.scheduling import LocalRunner, SSHRunner
    if host in ("localhost", "local"):
        return LocalRunner(host=host)
    return SSHRunner(host=host)


def _backend_for(name: str):
    """Resolve a backend name (from `--backend`) to a backend instance."""
    from axiom.cli.scheduling import (
        CronBackend, LaunchdBackend, SystemdTimerBackend,
        WindowsTaskSchedulerBackend,
    )
    if name == "cron":
        return CronBackend()
    if name == "launchd":
        return LaunchdBackend()
    if name == "systemd":
        return SystemdTimerBackend()
    if name == "wintasks":
        return WindowsTaskSchedulerBackend()
    raise ValueError(f"unsupported backend: {name!r}")


def _other_backend_artifacts(
    project_root: Path, host: str, current_backend: str,
) -> list[dict]:
    """Return schedule rows for `host` whose backend doesn't match
    `current_backend`. Used to surface the create/install mismatch
    (user wrote .cron, asked install --backend launchd → empty)."""
    return [
        row for row in list_schedules(project_root)
        if row["host"] == host and row["backend"] != current_backend
    ]


def _resolve_backend_name(*, requested: str, runner, dry_run: bool) -> str:
    """Translate ``--backend auto`` into a concrete backend name by
    probing the host. Anything other than ``auto`` passes through.

    Dry-run short-circuits to ``cron`` without probing the runner —
    dry-run promises no host-side calls, so detection has to wait.
    Users who want auto-detection during dry-run can pass an explicit
    ``--backend`` instead.
    """
    if requested != "auto":
        return requested
    if dry_run:
        return "cron"
    from axiom.cli.scheduling import detect_backend
    return detect_backend(runner)


def report_backend_name(backend) -> str:
    return getattr(backend, "name", type(backend).__name__)


if __name__ == "__main__":
    import sys
    sys.exit(main())
