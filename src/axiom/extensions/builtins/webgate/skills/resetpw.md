# SKILL: gate.resetpw

**Owner:** `axi gate resetpw` · invocable through SkillRegistry (ADR-056)
**Kind:** skill (function-backed)
**Status:** active

## What this skill does

The admin-mediated half of "forgot password": rotates an existing account's
password in one command, preserving roles and name. A running gate picks up the
new hash on the next login via the file store's mtime reload — no restart. See
`resetpw.py`. Reachable via `ctx.registry.invoke("gate.resetpw", params, ctx)`.

## Inputs / Outputs

`email` (required, must already exist — never creates an account); optional
`password` (generated + shown once if omitted). Returns a uniform `SkillResult`;
`value.password` carries a generated password exactly once.

## Safety

Refuses to reset an unknown account (no create-by-side-effect). Writes are
validated before touching the good file. A generated password is printed once —
relay it over a secure channel.
