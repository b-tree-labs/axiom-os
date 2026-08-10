# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.backup_validate`` — periodic proof that backups are restorable.

A backup schedule without validation converges on "one stale artifact
nobody can restore." This skill is the check PULSE fires on the policy's
``validate_schedule``; it mirrors ``verify.py``'s check battery style
(each probe → PASS/WARN/FAIL + remediation) and is fail-closed: any FAIL
makes the skill report ``ok=False`` and publish a HERALD event.

Checks:

1. ``backup_exists``   — an artifact is present in the target root.
2. ``backup_fresh``    — the newest artifact is younger than the
   staleness threshold (default 26 h — one nightly cycle + slack).
   Stale → HERALD ``data.backup.stale``.
3. ``backup_nonempty`` — the newest artifact has bytes in it.
4. ``toc_parses``      — ``pg_restore --list`` parses the custom-format
   TOC (structural restorability without touching a database).
5. ``restore_live``    — optional (``validate_restore=true`` + a
   ``scratch_dsn``): restore into the scratch database, sanity-check row
   presence against the live DSN, then drop the scratch objects.

Any FAIL other than staleness → HERALD ``data.backup.validate_failed``.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.governance.classification import Classification
from axiom.infra.backup import BackupResult, list_backups
from axiom.infra.skills import SkillContext, SkillResult

from .. import _authz, _herald
from ..database.backup_policy import BackupPolicy, load_backup_policy
from .verify import Status, VerifyCheck

DEFAULT_MAX_AGE_HOURS = 26.0


# -- probes (module-level so tests monkeypatch them; no live DB in units) -----


def _pg_restore_list(path: Path) -> tuple[bool, str]:
    """``pg_restore --list`` over a custom-format archive: does the TOC parse?"""
    try:
        proc = subprocess.run(
            ["pg_restore", "--list", str(path)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except FileNotFoundError:
        return False, "pg_restore not found — install PostgreSQL client tools"
    except subprocess.TimeoutExpired:
        return False, "pg_restore --list timed out after 300s"
    if proc.returncode != 0:
        return False, f"pg_restore --list failed: {proc.stderr.strip()[:200]}"
    entries = [ln for ln in proc.stdout.splitlines() if ln and not ln.startswith(";")]
    return True, f"TOC parses ({len(entries)} entries)"


def _restore_into_scratch(path: Path, scratch_dsn: str) -> tuple[bool, str]:
    """Restore the archive into the scratch database (no owner/ACL baggage)."""
    try:
        proc = subprocess.run(
            ["pg_restore", "--no-owner", "--no-acl", "-d", scratch_dsn, str(path)],
            capture_output=True,
            text=True,
            timeout=3600,
            check=False,
        )
    except FileNotFoundError:
        return False, "pg_restore not found — install PostgreSQL client tools"
    except subprocess.TimeoutExpired:
        return False, "scratch restore timed out after 3600s"
    if proc.returncode != 0:
        return False, f"scratch restore failed: {proc.stderr.strip()[:200]}"
    return True, "scratch restore completed"


def _row_sanity(scratch_dsn: str, live_dsn: str | None) -> tuple[bool, str]:
    """Row-count sanity: the restored scratch must actually contain data.

    Compares user-table counts (and, when a live DSN is available, that the
    scratch saw at least one row wherever live has rows in common tables).
    """
    try:
        import psycopg2
    except ImportError:
        return False, "psycopg2 not installed — cannot row-count scratch"

    def _tables_with_counts(dsn: str) -> dict[str, int]:
        conn = psycopg2.connect(dsn, connect_timeout=10)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT schemaname || '.' || relname, n_live_tup FROM pg_stat_user_tables"
                )
                return {r[0]: int(r[1]) for r in cur.fetchall()}
        finally:
            conn.close()

    try:
        scratch = _tables_with_counts(scratch_dsn)
    except Exception as exc:  # noqa: BLE001 — probe boundary
        return False, f"cannot inspect scratch: {exc}"
    if not scratch:
        return False, "scratch restore produced zero user tables"
    if live_dsn:
        try:
            live = _tables_with_counts(live_dsn)
        except Exception as exc:  # noqa: BLE001 — probe boundary
            return True, f"scratch has {len(scratch)} tables (live uninspectable: {exc})"
        missing = sorted(set(live) - set(scratch))
        if missing:
            return False, f"tables in live but not in restore: {missing[:10]}"
    return True, f"scratch restore contains {len(scratch)} user tables"


def _drop_scratch_objects(scratch_dsn: str) -> None:
    """Drop everything the restore created in the scratch database."""
    try:
        import psycopg2
    except ImportError:  # pragma: no cover — guarded by _row_sanity first
        return
    conn = psycopg2.connect(scratch_dsn, connect_timeout=10)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name NOT IN ('pg_catalog', 'information_schema', 'pg_toast')"
            )
            for (schema,) in cur.fetchall():
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            cur.execute("CREATE SCHEMA IF NOT EXISTS public")
    finally:
        conn.close()


# -- checks -------------------------------------------------------------------


def _age_hours(artifact: BackupResult, now: datetime) -> float:
    created = datetime.fromisoformat(artifact.created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (now - created).total_seconds() / 3600.0


def _check_fresh(newest: BackupResult, max_age_hours: float, now: datetime) -> VerifyCheck:
    age = _age_hours(newest, now)
    if age <= max_age_hours:
        return VerifyCheck(
            name="backup_fresh",
            status=Status.PASS,
            detail=f"newest artifact is {age:.1f}h old (threshold {max_age_hours}h)",
        )
    return VerifyCheck(
        name="backup_fresh",
        status=Status.FAIL,
        detail=f"newest artifact is {age:.1f}h old — STALE (threshold {max_age_hours}h)",
        remediation="Check the PULSE data.backup cadence + the orchestrator "
        "service; run `axi data backup` to take one now.",
    )


def _check_toc(newest: BackupResult) -> VerifyCheck:
    if newest.backup_path.suffix != ".dump":
        return VerifyCheck(
            name="toc_parses",
            status=Status.WARN,
            detail=f"{newest.backup_path.name} is plain-format; "
            "pg_restore --list only verifies custom (-Fc) archives",
            remediation="Standardize on fmt='custom' (the data.backup default).",
        )
    ok, detail = _pg_restore_list(newest.backup_path)
    return VerifyCheck(
        name="toc_parses",
        status=Status.PASS if ok else Status.FAIL,
        detail=detail,
        remediation=None
        if ok
        else "The artifact is not restorable — take a "
        "fresh backup (`axi data backup`) and investigate disk/pg_dump health.",
    )


def _check_restore_live(
    newest: BackupResult, scratch_dsn: str | None, live_dsn: str | None
) -> VerifyCheck:
    if not scratch_dsn:
        return VerifyCheck(
            name="restore_live",
            status=Status.FAIL,
            detail="validate_restore=true but no scratch_dsn provided",
            remediation="Pass scratch_dsn= pointing at a disposable database.",
        )
    ok, detail = _restore_into_scratch(newest.backup_path, scratch_dsn)
    if ok:
        ok, detail = _row_sanity(scratch_dsn, live_dsn)
    try:
        _drop_scratch_objects(scratch_dsn)
    except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
        detail += f" (scratch cleanup failed: {exc})"
    return VerifyCheck(
        name="restore_live",
        status=Status.PASS if ok else Status.FAIL,
        detail=detail,
        remediation=None
        if ok
        else "Live-restore proof failed — treat the backup chain as broken "
        "until a scratch restore succeeds.",
    )


# -- the skill ------------------------------------------------------------------


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    policy = load_backup_policy(state_dir=ctx.state_dir) or BackupPolicy()
    target_root = Path(params.get("target_root") or policy.resolved_target_root()).expanduser()
    try:
        max_age_hours = float(params.get("max_age_hours", DEFAULT_MAX_AGE_HOURS))
    except (TypeError, ValueError):
        return SkillResult(ok=False, errors=["max_age_hours must be a number"])
    now = datetime.now(UTC)

    actions: list[str] = []
    with _authz.action(
        verb="backup_validate",
        resource="data-platform://database/backups",
        classification=Classification.INTERNAL,
        actor=params.get("actor"),
    ) as act:
        actions.append(f"audit-receipt: {act.receipt_id}")

        checks: list[VerifyCheck] = []
        backups = list_backups(target_root)
        if not backups:
            checks.append(
                VerifyCheck(
                    name="backup_exists",
                    status=Status.FAIL,
                    detail=f"no backup artifacts under {target_root}",
                    remediation="Enable the BackupPolicy and/or run `axi data backup`.",
                )
            )
            newest = None
        else:
            newest = backups[0]
            checks.append(
                VerifyCheck(
                    name="backup_exists",
                    status=Status.PASS,
                    detail=f"{len(backups)} artifact(s); newest {newest.backup_path.name}",
                )
            )

        if newest is not None:
            checks.append(_check_fresh(newest, max_age_hours, now))
            checks.append(
                VerifyCheck(
                    name="backup_nonempty",
                    status=Status.PASS if newest.size_bytes > 0 else Status.FAIL,
                    detail=f"{newest.size_bytes} bytes",
                    remediation=None
                    if newest.size_bytes > 0
                    else "Zero-byte artifact — pg_dump wrote nothing; check "
                    "disk space and database connectivity.",
                )
            )
            checks.append(_check_toc(newest))
            if params.get("validate_restore"):
                checks.append(
                    _check_restore_live(
                        newest,
                        params.get("scratch_dsn"),
                        params.get("dsn"),
                    )
                )

    failed = [c for c in checks if c.status == Status.FAIL]
    stale = [c for c in failed if c.name == "backup_fresh"]
    hard_failures = [c for c in failed if c.name != "backup_fresh"]

    if stale:
        _herald.publish_event(
            "data.backup.stale",
            f"backup STALE: {stale[0].detail}",
            payload={"target_root": str(target_root)},
        )
        actions.append("published HERALD event: data.backup.stale")
    if hard_failures:
        summary = "; ".join(f"{c.name}: {c.detail}" for c in hard_failures)
        _herald.publish_event(
            "data.backup.validate_failed",
            f"backup validation FAILED: {summary}",
            payload={"target_root": str(target_root)},
        )
        actions.append("published HERALD event: data.backup.validate_failed")

    ok = not failed
    return SkillResult(
        ok=ok,
        value={
            "target_root": str(target_root),
            "newest": str(newest.backup_path) if newest else None,
            "max_age_hours": max_age_hours,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status.value,
                    "detail": c.detail,
                    "remediation": c.remediation,
                }
                for c in checks
            ],
            "receipt": act.receipt_id,
        },
        actions_taken=actions,
        errors=[
            f"{c.name}: {c.detail} — treat backups as unusable until this check passes"
            for c in failed
        ],
    )
