# SKILL: gate.reinstate

**Owner:** `axi gate reinstate` · invocable through SkillRegistry (ADR-056)
**Kind:** skill (function-backed)
**Status:** active

## What this skill does

Lifts a suspension (`axi gate reinstate ana@site`). The session and keys the person
already holds work again on their next request: they do not sign in again and
do not fetch a new key.

## Safety

Touches only the suspension attributes. Re-running is a no-op.
