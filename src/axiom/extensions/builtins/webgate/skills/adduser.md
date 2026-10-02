# SKILL: gate.adduser

**Owner:** `axi gate adduser` · invocable through SkillRegistry (ADR-056)
**Kind:** skill (function-backed)
**Status:** active

## What this skill does

Adds a password account to the gate's accounts file (`--accounts-file` or
`$AXIOM_GATE_USERS_FILE`): hashes the password (scrypt), validates the record,
and appends it atomically. A running gate picks the account up on the next
login via the file store's mtime reload — no restart. See `adduser.py` for the
body. Reachable from any persona via
`ctx.registry.invoke("gate.adduser", params, ctx)`.

## Inputs / Outputs

`email` (required); optional `role` (repeatable), `name`, `password` (a strong
one is generated + shown once if omitted), `user_id`, `force` (overwrite).
Returns a uniform `SkillResult`; `value.password` carries a generated password
exactly once.

## Safety

Writes are validated before touching the good file (temp-then-rename); a bad
record never clobbers existing accounts. Passwords are never stored in
plaintext. A generated password is printed once — relay it over a secure
channel.
