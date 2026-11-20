# SKILL: gate.revoke

**Owner:** `axi gate revoke api-key <key-id>` / `axi gate revoke invitation <invitation-id>` · invocable through SkillRegistry (ADR-056)
**Kind:** skill (function-backed)
**Status:** active

## What this skill does

Revokes an issued API key (or, with `invitation`, an invitation nobody has redeemed: its code is refused as revoked from then on): stamps `revoked_at` on the record in the keys file
(`--keys-file` or `$AXIOM_GATE_API_KEYS_FILE`). The atomic rewrite bumps the
mtime the authz hook's store watches, so the very next request presenting the
key is denied — revocation is immediate, no restart, no cache to expire. See
`revoke_key.py` for the body. Reachable from any persona via
`ctx.registry.invoke("gate.revoke", params, ctx)`.

## Inputs / Outputs

`resource` (`api-key` or `invitation`), `key_id` / `invitation_id` (required — from
issuance output or `gate.list` with resource `api-keys` / `invitations`), optional
`keys_file` / `invitations_file`. Returns a uniform
`SkillResult` with the revocation timestamp. Idempotent: re-revoking reports
the original `revoked_at`.

## Safety

Revocation never deletes the record — the audit trail of what was issued to
whom, with which scopes, is preserved. An unknown key id is an error (exit
nonzero), so a typo cannot silently "succeed".
