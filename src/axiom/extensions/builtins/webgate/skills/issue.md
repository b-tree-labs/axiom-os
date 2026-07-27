# SKILL: gate.issue

**Owner:** `axi gate issue api-key` · invocable through SkillRegistry (ADR-056)
**Kind:** skill (function-backed)
**Status:** active

## What this skill does

Issues a bearer API key bound to a NON-human API principal
(`@name:context`), for the `/v1` uniform-authz surface: mints an
auto-generated key id + high-entropy secret, stores only the scrypt hash in
the keys file (`--keys-file` or `$AXIOM_GATE_API_KEYS_FILE`), and returns the
plaintext token exactly once. The composed app's authz hook resolves the key
live via mtime hot-reload — no restart. See `issue_key.py` for the body.
Reachable from any persona via `ctx.registry.invoke("gate.issue", params, ctx)`.

## Inputs / Outputs

`resource` (must be `api-key`), `principal` (required, matrix-style
`@name:context`), `scope` (required, repeatable — `MOUNT[:VERB]` with VERB one
of `read`/`invoke`/`access`; no default grant), optional `name`, `keys_file`.
Returns a uniform `SkillResult`; `value.token` carries the plaintext key
exactly once; `value.key_id` is the handle for `gate.revoke`.

## Safety

Keys are hashed at rest (webauth's self-describing scrypt scheme); the file
never contains a plaintext token. Issuance is least-privilege: at least one
scope is required and the authz hook enforces scopes fail-closed. Relay the
printed token over a secure channel — it cannot be recovered later, only
revoked and reissued.
