# ADR-003 (schedule): One engine for cron + interval + trigger

**Status:** Accepted (2026-05-31)
**Locks:** spec-axiom-schedule §3

## Context

PULSE supports four cadence kinds: `one_shot`, `interval`, `cron`, `trigger`. A naive implementation would build three or four separate tick loops with bespoke retry / idempotency / dead-letter wiring per kind.

## Decision

Every schedule row carries a unified `next_fire_at: datetime | None` column. The tick loop selects rows by `next_fire_at <= now` regardless of cadence kind. Cron + interval compute `next_fire_at` after every fire; trigger schedules carry `next_fire_at = NULL` until a separate matcher loop (PULSE-2) consults the event bus and writes `next_fire_at := now()` on match — at which point the tick loop picks them up like any other due row.

## Consequences

- **Operational visibility uniformity.** `axi schedule list` shows `next_fire_at` for every kind; trigger schedules display `next_fire_at: pending` until matched. The operator learns one mental model.
- **The retry / dead-letter / idempotency machinery is one code path.** Adding a fourth cadence kind in v3 only adds a new way to compute `next_fire_at`.
- **Test surface shrinks.** A synthetic-clock harness around a single `tick()` covers cron, interval, and trigger semantics with one fixture.
- **Cost:** Trigger schedules pay one extra `UPDATE` on match instead of being dispatched directly from the matcher. Negligible; consistency wins.
