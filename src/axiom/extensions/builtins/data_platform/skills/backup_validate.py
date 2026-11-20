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
   ``scratch_dsn``): recreate the scratch database, restore into it,
   sanity-check row presence against the live DSN, then drop the database.
   Dropping the whole database is the only cleanup that works on a
   TimescaleDB restore: its schemas belong to the extension and cannot be
   dropped one by one. A cleanup failure is a FAIL, because a scratch
   database left behind stacks the next restore on top of this one.

Any FAIL other than staleness → HERALD ``data.backup.validate_failed``.
"""

from __future__ import annotations

import re
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


def _brand_cli() -> str:
    """The command the operator actually typed.

    These lines said "axi" unconditionally, so a consumer distribution's CLI
    told the operator to run a command that does not exist on their machine.
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


DEFAULT_MAX_AGE_HOURS = 26.0

#: Every PostgreSQL server has it, so the scratch database can be managed from outside.
ADMIN_DB = "postgres"

#: TimescaleDB's own storage. A chunk made after the dump exists live and not in
#: the restore, which says nothing about the backup; a hypertable's parent is
#: what the comparison needs, and it lives in the user's schema.
_INTERNAL_PREFIXES = ("_timescaledb_internal.",)

_FK_VIOLATION = re.compile(r'violates foreign key constraint "([^"]+)"')


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


def _admin_dsn(scratch_dsn: str) -> str:
    """The same server, connected to the maintenance database.

    A database cannot be dropped or created from inside itself.
    """
    return scratch_dsn_for(scratch_dsn, ADMIN_DB)


def _scratch_name(scratch_dsn: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(scratch_dsn).path.lstrip("/")


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _recreate_scratch(scratch_dsn: str, live_dsn: str | None) -> None:
    """Replace the scratch database with an empty one, ready for this restore.

    Refuses to touch the database the node runs on. A TimescaleDB source gets
    the extension at the SOURCE's version: the dump would otherwise create it at
    the image's default, and an image can name a default it ships no script for.
    """
    import psycopg2

    name = _scratch_name(scratch_dsn)
    if not name or name == ADMIN_DB:
        raise ValueError(f"not a disposable database: {name!r}")
    if live_dsn and name == _scratch_name(live_dsn):
        raise ValueError(f"scratch database {name!r} is the live database")
    _drop_scratch_database(scratch_dsn)

    conn = psycopg2.connect(_admin_dsn(scratch_dsn), connect_timeout=10)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE {_quote_ident(name)}")
    finally:
        conn.close()

    version = _timescaledb_version(live_dsn) if live_dsn else None
    if version:
        conn = psycopg2.connect(scratch_dsn, connect_timeout=10)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION timescaledb VERSION %s", (version,))
                cur.execute("SELECT timescaledb_pre_restore()")
        finally:
            conn.close()


def _timescaledb_version(dsn: str) -> str | None:
    import psycopg2

    conn = psycopg2.connect(dsn, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
            row = cur.fetchone()
            return str(row[0]) if row else None
    finally:
        conn.close()


def _finish_restore(scratch_dsn: str) -> None:
    """Leave TimescaleDB restore mode, so the row check reads a working database."""
    import psycopg2

    conn = psycopg2.connect(scratch_dsn, connect_timeout=10)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
            if cur.fetchone():
                cur.execute("SELECT timescaledb_post_restore()")
    finally:
        conn.close()


def _fks_onto_excluded(dsn: str, excluded: tuple[str, ...]) -> frozenset[str]:
    """Foreign keys pointing at a table whose data the backup leaves out on purpose.

    Such a key cannot be restored until that table is re-derived, so its failure
    is what the policy asked for, not a broken backup. A name that resolves to no
    table matches nothing, which keeps the check failing closed.
    """
    if not excluded:
        return frozenset()
    import psycopg2

    conn = psycopg2.connect(dsn, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT conname FROM pg_constraint WHERE contype = 'f' AND confrelid IN "
                "(SELECT to_regclass(t) FROM unnest(%s::text[]) AS t)",
                (list(excluded),),
            )
            return frozenset(str(r[0]) for r in cur.fetchall())
    finally:
        conn.close()


def _judge_restore_errors(stderr: str, expected_fks: frozenset[str]) -> tuple[bool, str]:
    """A non-zero pg_restore: did anything go wrong beyond the expected foreign keys?"""
    errors = [ln for ln in stderr.splitlines() if ln.startswith("pg_restore: error:")]
    skipped: list[str] = []
    other: list[str] = []
    for line in errors:
        match = _FK_VIOLATION.search(line)
        if match and match.group(1) in expected_fks:
            skipped.append(match.group(1))
        else:
            other.append(line)
    if errors and not other:
        return True, (
            f"scratch restore completed; foreign keys not restored because the backup "
            f"leaves out the data they point at: {', '.join(sorted(skipped))}"
        )
    return False, f"scratch restore failed: {('; '.join(other) or stderr.strip())[:400]}"


def _restore_into_scratch(
    path: Path, scratch_dsn: str, expected_fks: frozenset[str] = frozenset()
) -> tuple[bool, str]:
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
        return _judge_restore_errors(proc.stderr, expected_fks)
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
        missing = sorted(
            t for t in set(live) - set(scratch) if not t.startswith(_INTERNAL_PREFIXES)
        )
        if missing:
            return False, f"tables in live but not in restore: {missing[:10]}"
    return True, f"scratch restore contains {len(scratch)} user tables"


def _drop_scratch_database(scratch_dsn: str) -> None:
    """Drop the scratch database whole. Raises when it cannot; the caller reports it."""
    import psycopg2

    name = _scratch_name(scratch_dsn)
    conn = psycopg2.connect(_admin_dsn(scratch_dsn), connect_timeout=10)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {_quote_ident(name)} WITH (FORCE)")
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
        f"service; run `{_brand_cli()} data backup` to take one now.",
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
        f"fresh backup (`{_brand_cli()} data backup`) and investigate disk/pg_dump health.",
    )


def scratch_dsn_for(live_dsn: str, scratch_db: str) -> str:
    """The live DSN with its database name swapped for *scratch_db*.

    So a policy can name a disposable database without persisting a second copy
    of the credentials. `axi data backup-policy` prints the policy back, and a
    password does not belong in that output or in the file it reads.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(live_dsn)
    return urlunsplit(parts._replace(path=f"/{scratch_db.lstrip('/')}"))


def _check_restore_live(
    newest: BackupResult,
    scratch_dsn: str | None,
    live_dsn: str | None,
    excluded_tables: tuple[str, ...] = (),
) -> VerifyCheck:
    if not scratch_dsn:
        return VerifyCheck(
            name="restore_live",
            status=Status.FAIL,
            detail="validate_restore=true but no scratch_dsn provided",
            remediation="Pass scratch_dsn= pointing at a disposable database.",
        )
    try:
        expected_fks = _fks_onto_excluded(live_dsn, excluded_tables) if live_dsn else frozenset()
        _recreate_scratch(scratch_dsn, live_dsn)
    except Exception as exc:  # noqa: BLE001 — probe boundary
        return VerifyCheck(
            name="restore_live",
            status=Status.FAIL,
            detail=f"could not prepare the scratch database: {exc}",
            remediation="This backup is unproven. The account needs CREATEDB and the scratch "
            "database must be disposable.",
        )
    ok, detail = _restore_into_scratch(newest.backup_path, scratch_dsn, expected_fks)
    if ok:
        restored = detail
        try:
            _finish_restore(scratch_dsn)
            ok, sanity = _row_sanity(scratch_dsn, live_dsn)
            detail = f"{restored}; {sanity}" if ok else sanity
        except Exception as exc:  # noqa: BLE001 — probe boundary
            ok, detail = False, f"restored, but could not leave restore mode: {exc}"
    try:
        _drop_scratch_database(scratch_dsn)
    except Exception as exc:  # noqa: BLE001 — reported, never swallowed
        ok = False
        detail += f"; scratch database NOT dropped, it holds disk until removed: {exc}"
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
                    remediation=f"Enable the BackupPolicy and/or run `{_brand_cli()} data backup`.",
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
            # Params win; otherwise the policy decides. Absent both, the policy
            # default is ON — a validation that omits the restore is the exact
            # report that passed on an unrestorable artifact.
            asked_explicitly = params.get("validate_restore") is not None
            want_restore = params.get("validate_restore")
            if want_restore is None:
                want_restore = policy.validate_restore
            # The database this node actually uses. Previously only
            # params["dsn"], so the row-count comparison was skipped unless
            # someone passed one by hand — and a scheduled run never does.
            from .._dsn import resolve_dsn

            live_dsn = resolve_dsn(params)

            scratch = params.get("scratch_dsn") or policy.scratch_dsn
            # A NAME is preferred over a DSN: the credentials are the live
            # ones, so nothing is written into (or printed back out of) the
            # policy file. See scratch_dsn_for().
            scratch_db = params.get("scratch_db") or policy.scratch_db
            if not scratch and scratch_db and live_dsn:
                scratch = scratch_dsn_for(live_dsn, scratch_db)

            if want_restore and not scratch and not asked_explicitly:
                # On by default with nowhere to restore INTO. That is unproven,
                # not broken, and the two deserve different colours: FAIL means
                # the backup does not restore, WARN means nobody has checked.
                # Conflating them is how a red report becomes background noise.
                checks.append(
                    VerifyCheck(
                        name="restore_live",
                        status=Status.WARN,
                        detail="restore not attempted — no scratch_dsn configured, "
                        "so this backup is unproven",
                        remediation=(
                            "Set scratch_db in the backup policy to a disposable database "
                            "name on the same server (preferred — no credential in the file), "
                            "or scratch_dsn to a full DSN, "
                            "database. Four structural checks passed on an artifact "
                            "that could not be restored; only this one catches that."
                        ),
                    )
                )
            elif not want_restore:
                # Never silently absent. A report with four PASS rows and no
                # mention of restoring reads as complete, and is not.
                checks.append(
                    VerifyCheck(
                        name="restore_live",
                        status=Status.WARN,
                        detail="restore not attempted — this backup is unproven",
                        remediation=(
                            "Set validate_restore = true and scratch_dsn in the backup "
                            "policy. Structural checks passed on an artifact that could "
                            "not be restored; only this one catches that."
                        ),
                    )
                )
            else:
                checks.append(
                    _check_restore_live(
                        newest,
                        scratch,
                        live_dsn,
                        tuple(policy.exclude_table_data or ()),
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
