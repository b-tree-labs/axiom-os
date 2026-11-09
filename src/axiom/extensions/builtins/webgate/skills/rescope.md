# SKILL: gate.rescope

**Owner:** `axi gate rescope api-key <key-id> [--site S] [--add-scope X] [--remove-scope Y]` · invocable through SkillRegistry (ADR-056)
**Kind:** skill (function-backed)
**Status:** active

## What this skill does

Changes an issued API key's site or scopes in place. The record's `key_id`,
`principal` and `secret_hash` are kept exactly, so the token its holder already
has keeps authenticating; it is held to the new site or scopes on its next
request (the authz store reloads on the atomic rewrite). Use it to move a holder
to a narrower rule without asking them to do anything. See `rescope_key.py`.

## Inputs / Outputs

`resource` (must be `api-key`), `key_id` (required), and at least one of `site`,
`add_scope` (repeatable), `remove_scope` (repeatable); optional `keys_file`.
Returns the key's site and scopes before and after. Idempotent: a change already
made reports `changed: false` and writes nothing.

## Safety

A key already tied to a site cannot be moved to another (issue a key for that
site instead). A revoked key, an unknown key id, and removing a key's last scope
are refused. The verb is mutating, so the universal action audit records it.
Widening a key (adding `*`, the owner's grant) reads every site: do it only with
the owner's say-so.
