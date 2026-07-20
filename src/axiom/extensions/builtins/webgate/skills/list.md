# SKILL: gate.list

**Owner:** `axi gate list [accounts|api-keys]` · invocable through SkillRegistry (ADR-056)
**Kind:** skill (function-backed)
**Status:** active

## What this skill does

Lists the gate's credentials by resource: `accounts` (the default — email,
name, roles, user_id) or `api-keys` (key id, principal, scopes, name,
created/revoked timestamps) — never hashes of either kind. A not-yet-created
file lists as empty (deny-all) rather than erroring, so an admin can check
state before provisioning. See `list_users.py`. Reachable via
`ctx.registry.invoke("gate.list", params, ctx)`.

## Inputs / Outputs

Optional `resource` (`accounts` default, or `api-keys`), `accounts_file`
(default `$AXIOM_GATE_USERS_FILE`), `keys_file` (default
`$AXIOM_GATE_API_KEYS_FILE`). Returns a uniform `SkillResult` whose
`value.items` is a list of summaries.

## Safety

Read-only. Password hashes and API-key secret hashes are never included in
the output; plaintext tokens are never recoverable after issuance.
