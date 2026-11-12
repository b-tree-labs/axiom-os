# SKILL: gate.suspend

**Owner:** `axi gate suspend` · invocable through SkillRegistry (ADR-056)
**Kind:** skill (function-backed)
**Status:** active

## What this skill does

Suspends one account (`axi gate suspend ana@site --reason "left the project"`).
From the next request every session the person holds answers as signed out and
every key they hold is refused. The mark is kept in the account's attributes
(`suspended_at`, `suspended_by`, `suspended_reason`).

## Safety

Nothing is revoked and nothing is re-issued, so `gate reinstate` restores the same
session and the same keys. Re-running is a no-op. Never prints a key or a hash.
