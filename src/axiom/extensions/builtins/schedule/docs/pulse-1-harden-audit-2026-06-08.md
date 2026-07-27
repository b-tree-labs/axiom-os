# PULSE-1 Harden — Audit & Build Plan (2026-06-08)

Audit of the deployed `schedule` (PULSE) extension before hardening. Part of the
three-PR scheduling chunk: **(1) harden PULSE-1**, (2) a domain-agnostic
scheduling seam a consumer CLI verb wraps (ADR-056), (3) a CalDAV-first calendar
connector behind the connector seam (M365 Graph OAuth-blocked, ref #366).

## Current state — scaffolding, not a working fire path

| Piece | State |
|---|---|
| `db_models` (definition / fire_log / lease) | ✅ Solid — idempotency unique constraint, `dead_letter` outcome, `retry_policy`, `capability_envelope`, classification ceiling, RACI default |
| `cadence.compute_next_fire_at` | ✅ Implemented — interval / cron (`croniter`) / one_shot / jitter / `not_after` |
| `manifest.parse_manifest_block` | ✅ Implemented + tested |
| `lease` (single-node) | ✅ In-memory single-node acquire/renew works (Postgres advisory-lock variant is the PULSE-1→2 path) |
| skill shells (`register/pause/.../status`) | ⚠️ Present but return `ok=False` "wiring in progress" |
| **`engine._pull_due` / `engine._fire_one`** | ❌ `NotImplementedError` — the fire loop does not run |
| **`api.*` (register/pause/resume/cancel/list/status/fire_now)** | ❌ All `NotImplementedError` |
| **DB test harness** | ❌ None — no conftest; `session_for` is the prod Postgres path, untested in units |
| **persona** | ✅ Added this PR (`agents/pulse/persona.md`; manifest already referenced it) |

The 36 green tests exercise **control flow + contracts only** — their own
docstrings say the integration path "lands once the DB harness is wired."

## Build plan (PR-1 = this branch, `feat/pulse-1-harden`)

TDD-first, each step a failing test then impl:

1. **DB harness (conftest).** SQLite in-memory session injected into engine/api.
   The models are schema-unqualified in `__table_args__`, so
   `Base.metadata.create_all(sqlite_engine)` works; prod stays Postgres via
   `session_for("schedule")`. Make `api.*` take an injectable session provider
   (default `session_for`) so units don't need Postgres.
2. **`api.register`** → writes a `ScheduleDefinition`, computes initial
   `next_fire_at` via `compute_next_fire_at`, returns `ScheduleId`. Reject
   `trigger` (already tested). Skill `register.run` becomes a thin wrapper.
3. **`engine._pull_due`** → query active rows `next_fire_at <= now`.
4. **`engine._fire_one`** → claim idempotency slot → `authz.decide` →
   executor.run → record `success`; on failure, retry per `retry_policy`;
   on exhaustion → `dead_letter`; advance `next_fire_at` via cadence.
5. **`pause/resume/cancel/list/status/fire_now`** + thin skill wrappers + CLI
   subprocess smoke. Each capability is one skill-fn surfaced as CLI verb + MCP
   tool (ADR-056) — no logic in argparse.
6. **Integration tests** (against the SQLite harness): register→tick→fire→receipt;
   idempotent replay (same instant fires once); retry-then-success;
   retry-exhaustion→`dead_letter`; authz-deny records `failed`/`skipped`, no exec.

## PR-2 / PR-3 (separate branches)

- **PR-2 — scheduling seam:** a domain-agnostic `(params, ctx) -> SkillResult`
  contract (`register_slot` / `record_actual` / `register_cadence` / `slot_status`,
  opaque metadata) a consumer CLI verb wraps. No consumer naming in Axiom code.
  Proven with one real cadence end-to-end. (See axiom-os#480, spec #481.)
- **PR-3 — CalDAV calendar connector:** on the connector framework, `detect()`
  existing state (ADR-068), two-way sync, feed/read PULSE cadences. M365 Graph is
  a second provider behind the same seam, OAuth-blocked for now (#366).

_Copyright (c) 2026 The University of Texas at Austin. Apache-2.0 licensed._
