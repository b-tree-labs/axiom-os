# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data.backup_policy`` — what the node BELIEVES the policy is.

Not what the file says. The two came apart: `BackupPolicy` gained four fields
and the hand-written TOML loader was never taught them, so a policy file that
set three excluded tables and a 3600s timeout loaded as ``None`` and ``600``.
The file read correctly to a human and meant something else to the scheduler.

Arming the nightly schedule is done by editing that same file, so the arming
gesture and the configuration are the same gesture — there was no step in
between at which anyone would look. This is that step.

It therefore reports the **loaded** policy, and flags any key present in the
file that the loader did not carry into the dataclass. A setting that reads
correctly on disk and does nothing is the failure mode; an unknown key is how
it looks from here.

It also reports what is **actually scheduled**, which is a different fact from
what the policy asks for. `ensure_backup_cadences` runs once, at orchestrator
startup — nothing calls it when the file changes. So `enabled = true` written
into the file arms nothing until the service restarts, and until then the
policy and the schedule disagree with no sign of it anywhere. The PULSE rows
are the only thing that knows whether a backup will actually run.

``apply=True`` projects the policy onto PULSE immediately, so arming does not
require a service restart and can be confirmed in the same breath.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..database.backup_policy import BackupPolicy, backup_policy_path, load_backup_policy



def _redact_dsn(dsn: str) -> str:
    """``postgresql://user:***@host/db`` — enough to identify, not to use."""
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(dsn)
    except Exception:  # noqa: BLE001 - an unparseable value is fully hidden
        return "<redacted>"
    if not parts.password:
        return dsn
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit(parts._replace(netloc=f"{parts.username}:***@{host}"))

def _raw(path) -> tuple[set[str], set[str]]:
    """``(keys under [backup_policy], keys stranded at top level)``.

    Everything is read from the ``[backup_policy]`` table. A file whose
    settings sit at top level parses as valid TOML, loads as an empty table,
    and yields a policy of pure defaults — including ``enabled = false``. So
    the most dangerous version of this file is one where ``enabled = true`` is
    written, looks right, and arms nothing. Stranded keys are reported
    separately because that failure is invisible from the values alone.
    """
    import tomllib

    try:
        with open(path, "rb") as fh:
            doc = tomllib.load(fh)
    except Exception:  # noqa: BLE001 - an unreadable file is reported below
        return set(), set()
    table = doc.get("backup_policy")
    inside = set(table.keys()) if isinstance(table, dict) else set()
    stranded = {k for k, v in doc.items() if not isinstance(v, dict)}
    return inside, stranded



def _scheduled(state_dir) -> tuple[list[str], list[str], str]:
    """``(lines, problems, verdict)`` describing what PULSE will actually run.

    The policy is an intention. A schedule row is the effect. Reading the rows
    is the only way to answer "will a backup run tonight", and the two can
    disagree silently — `ensure_backup_cadences` runs at orchestrator startup
    and nothing re-runs it when the file changes.
    """
    from ..orchestration.service import BACKUP_ACTION, VALIDATE_ACTION

    try:
        from axiom.extensions.builtins.schedule import store as pulse_store
        from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition
    except ModuleNotFoundError as exc:  # the schedule extension is not installed
        missing = getattr(exc, "name", None) or str(exc)
        return ([], [f"cannot read PULSE schedules: {missing!r} is not installed"], "unknown")

    lines: list[str] = []
    problems: list[str] = []
    active = 0
    try:
        with pulse_store.session_scope() as sess:
            for action in (BACKUP_ACTION, VALIDATE_ACTION):
                rows = (
                    sess.query(ScheduleDefinition)
                    .filter(ScheduleDefinition.action == action)
                    .filter(ScheduleDefinition.state != "cancelled")
                    .order_by(ScheduleDefinition.created_at)
                    .all()
                )
                if not rows:
                    lines.append(f"   {action:<22} NO SCHEDULE ROW — will not run")
                    continue
                row = rows[0]
                # next_fire_at, not next_run_at. A getattr() default would
                # have turned the wrong name into a silent "no next run",
                # which is the same failure this verb exists to report.
                nxt = row.next_fire_at
                lines.append(
                    f"   {action:<22} {row.state}"
                    + (f", next {nxt}" if nxt else "")
                    + (f"  (+{len(rows) - 1} more rows)" if len(rows) > 1 else "")
                )
                if row.state == "active":
                    active += 1
    except Exception as exc:  # noqa: BLE001 — an unreachable schedule store is a report
        return ([], [f"cannot read PULSE schedules: {type(exc).__name__}: {exc}"], "unknown")

    verdict = "armed" if active == 2 else ("partial" if active else "not armed")
    return lines, problems, verdict

def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Show the effective backup policy and whether the file fully survived it."""
    # A str from argparse and a Path from SkillContext both arrive here.
    raw = params.get("state_dir") or getattr(ctx, "state_dir", None)
    state_dir = Path(raw) if raw else None
    path = backup_policy_path(state_dir=state_dir)

    policy = load_backup_policy(state_dir=state_dir)
    if policy is None:
        return SkillResult(
            ok=True,
            value={"path": str(path), "exists": False},
            actions_taken=[
                f"no policy at {path} — backups are not scheduled on this node. "
                "Defaults apply only to a manual `axi data backup`."
            ],
        )

    known = {f.name for f in dataclasses.fields(BackupPolicy)}
    present, stranded = _raw(path)
    ignored = sorted(present - known)

    lines = [f"policy: {path}"]
    for f in dataclasses.fields(BackupPolicy):
        value = getattr(policy, f.name)
        # A DSN carries a password and this output goes to a terminal, a log
        # and a SkillResult. Prefer `scratch_db` (a bare name); redact the
        # field that can hold a credential rather than relying on nobody
        # setting it.
        if f.name.endswith("_dsn") and value:
            value = _redact_dsn(str(value))
        default = f.default if f.default is not dataclasses.MISSING else None
        marker = "" if value == default else "  <- set"
        lines.append(f"   {f.name:<20} {value!r}{marker}")

    lines.append(
        "POLICY ASKS FOR: data.backup on "
        f"{policy.schedule!r}, data.backup_validate on {policy.validate_schedule!r}"
        if policy.enabled
        else "POLICY ASKS FOR: nothing — enabled = false"
    )

    # Applying is opt-in, and happens before the rows are read so the report
    # describes the state it leaves behind rather than the one it found.
    applied: dict[str, Any] | None = None
    if params.get("apply"):
        try:
            from ..orchestration.service import OrchestratorService

            applied = OrchestratorService(state_dir=state_dir).ensure_backup_cadences()
            lines.append(f"APPLIED: {applied}")
        except Exception as exc:  # noqa: BLE001 — report, do not raise at a CLI
            applied = {"error": f"{type(exc).__name__}: {exc}"}
            lines.append(f"APPLY FAILED: {applied['error']}")

    sched_lines, sched_problems, verdict = _scheduled(state_dir)
    lines.append(f"PULSE SAYS: {verdict}")
    lines.extend(sched_lines)

    errors: list[str] = []
    errors.extend(sched_problems)

    # The disagreement that has no other symptom. `enabled = true` in the file
    # and no active schedule row is what "I armed it" looks like when nothing
    # was armed — the policy is only read at orchestrator startup.
    if policy.enabled and verdict == "not armed":
        errors.append(
            "the policy is enabled but NOTHING is scheduled. ensure_backup_cadences "
            "runs at orchestrator startup and nothing re-runs it on a policy edit, "
            "so editing the file did not arm anything. Re-run with --apply, or "
            "restart the orchestrator."
        )
    elif policy.enabled and verdict == "partial":
        errors.append(
            "only one of the two cadences is active — a backup with no validation, "
            "or a validation with no backup. Re-run with --apply."
        )
    elif not policy.enabled and verdict == "armed":
        errors.append(
            "the policy is disabled but PULSE rows are still active — a disable "
            "that has not taken effect. Re-run with --apply, or restart the "
            "orchestrator."
        )

    if stranded:
        errors.append(
            f"{len(stranded)} setting(s) are at the top of the file instead of "
            f"under [backup_policy] and are ignored: {', '.join(sorted(stranded))}. "
            "The file parses, the table is empty, and every value above is a "
            "default — an `enabled = true` written this way arms nothing."
        )
    if ignored:
        errors.append(
            f"{len(ignored)} key(s) in the file the loader does not know and "
            f"silently dropped: {', '.join(ignored)}. A setting that reads "
            "correctly on disk and does nothing is invisible — either the key "
            "is misspelled or the loader is behind the dataclass."
        )

    return SkillResult(
        ok=not errors,
        value={
            "path": str(path),
            "exists": True,
            "policy": {
                k: (_redact_dsn(str(v)) if k.endswith("_dsn") and v else v)
                for k, v in dataclasses.asdict(policy).items()
            },
            "ignored_keys": ignored,
            "stranded_keys": sorted(stranded),
            "pulse": verdict,
            "applied": applied,
        },
        actions_taken=lines,
        errors=errors,
    )


__all__ = ["run"]
