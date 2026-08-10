# SKILL: data.backup_validate

**Owner:** `axi data backup-validate` · invocable through SkillRegistry (ADR-056)
**Kind:** skill (function-backed)
**Status:** active
**Last updated:** 2026-07-11

## What this skill does

Fail-closed proof that backups are usable, mirroring `verify.py`'s check
battery: newest artifact exists, is fresher than `max_age_hours`
(default 26 h), is non-empty, and its `pg_restore --list` TOC parses
(structural restorability). With `validate_restore=true` + a
`scratch_dsn` it additionally restores into the scratch database,
row-count-sanity-checks against live, and drops the scratch objects.

Staleness publishes HERALD `data.backup.stale`; any other FAIL publishes
`data.backup.validate_failed`. PULSE fires this skill on the policy's
`validate_schedule` cadence.

## Inputs / Outputs

Params: `target_root` (default `BackupPolicy.target_root`),
`max_age_hours` (default 26), `validate_restore`, `scratch_dsn`, `dsn`
(live, for row comparison), `actor`.
Returns a uniform `SkillResult` — `value.checks` carries each probe's
PASS/WARN/FAIL + remediation; `ok=false` on any FAIL.

## Safety

Wrapped in `_authz.action(verb="backup_validate", ...)`. Read-only
except the opt-in scratch restore, which only ever touches the
explicitly passed `scratch_dsn` and drops its restored objects
afterwards. It never writes to the live database.
