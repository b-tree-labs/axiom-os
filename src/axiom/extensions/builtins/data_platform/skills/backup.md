# SKILL: data.backup

**Owner:** `axi data backup` · invocable through SkillRegistry (ADR-056)
**Kind:** skill (function-backed)
**Status:** active
**Last updated:** 2026-07-11

## What this skill does

Policy-driven database backup: custom-format (`pg_dump -Fc`) dump of the
resolved DSN, schema-scoped per the persisted `BackupPolicy`, pruned to
`retention_count`, with an optional off-box replica through the
`BackupUploader` seam (Box chunked upload for >50 MB artifacts). On any
failure it publishes the HERALD event `data.backup.failed`.

CLI verb `axi data backup` is a thin wrapper; PULSE's scheduled dispatch
invokes the same skill (`data.backup`) on the policy's `schedule` cadence
when the policy is enabled.

## Inputs / Outputs

Params: `dsn` (default `DP1_RAG_DSN`/`DATABASE_URL`), `label`,
`target_root` (default `BackupPolicy.target_root`), `box_secret_ref`
(default `env://BOX_JWT_CONFIG`), `actor`.
Returns a uniform `SkillResult` — `value` carries the artifact path,
`size_bytes`, `pruned` count, the audit `receipt`, and off-box status.

## Safety

Wrapped in `_authz.action(verb="backup", ...)` — every run writes a
verdict receipt queryable via `axi audit`. The dump is read-only against
the database; the only mutations are artifact writes/prunes under
`target_root` and the optional Box upload. Restore stays interactive-only
(no skill) — the `restore` verb is reserved in `_authz` but deliberately
unwired.
