# Feature Flags — Technical Specification

**Status:** Active
**Date:** 2026-10-28
**Decision:** [ADR-210](../adrs/adr-210-a-feature-flag-is-declared-by-its-extension-and-resolved-default-env-node-account-user.md)
**Code:** `src/axiom/extensions/builtins/flags/`

---

## 1. Declaring a flag

In the owning extension's `axiom-extension.toml`:

```toml
[[flags]]
key = "control"                          # ^[a-z][a-z0-9_]{0,63}$
description = "The Control verb in the rail"
default = "off"                        # on | off | soon; true/false accepted
default_by_env = { development = "on" }
protected_in = ["production"]
```

Or, from a package that is not an extension directory, an entry point in the
`axiom.flags` group whose object is (or returns) a list of the same mappings.
A malformed declaration is logged and skipped; it never stops discovery.

## 2. Resolution

| Order (later wins) | Layer | Where it comes from |
|---|---|---|
| 1 | default | `default_by_env[env]`, else `default`, else `off` |
| 2 | env | `AXIOM_FLAG_<KEY>` (unparseable values are ignored) |
| 3 | node | `flag_overrides` row, scope `node`, id = `AXIOM_NODE_NAME` or short hostname |
| 4 | account | scope `account`, id = the session's `site` (else `$AXIOM_SITE`) |
| 5 | user | scope `user`, id = the session's `sub` |

`env` is `AXIOM_ENVIRONMENT`, else `ENVIRONMENT`, else `development`. In an
environment listed in `protected_in`, layers 3 to 5 are skipped and `flags.set`
refuses the flag. An undeclared key resolves to `off`.

## 3. Storage

Schema `flags` (ADR-052), table `flag_overrides`:

| Column | Type | Note |
|---|---|---|
| `scope` | varchar(16), PK | `node` \| `account` \| `user` (check constraint) |
| `scope_id` | varchar(255), PK | the node, account or user |
| `key` | varchar(64), PK | indexed |
| `value` | varchar(8) | `on` \| `off` \| `soon` (check constraint) |
| `updated_by` | varchar(255) | the principal that set it |
| `updated_at` | timestamptz | |

Migration: `flags/migrations/versions/0001_flag_overrides.py`. Only
`flags/store.py` opens a session on it.

## 4. Skills and surfaces

| Skill | Risk | Inputs | Returns |
|---|---|---|---|
| `flags.list` | read | `prefix?` | environment; per flag: default, protected, owner, description, stored overrides |
| `flags.resolve` | read | `key?`, `node?`, `account?`, `user?` | environment, `for`, `flags` (key → value), `sources` (key → layer) |
| `flags.set` | reversible-write | `key`, `value` (`on`/`off`/`soon`/`clear`), `scope` (default `node`), `scope_id?` | the row written, or `cleared` |

| Surface | Form |
|---|---|
| CLI | `axi flags [list]`, `axi flags resolve [KEY] [--node] [--account] [--user]`, `axi flags set KEY VALUE [--scope] [--id]`, `axi flags clear KEY [--scope] [--id]`, `--json` |
| MCP | `axiom_flags__list`, `axiom_flags__resolve`, `axiom_flags__set` |
| HTTP | `GET /api/v1/flags`: `{flags, sources, environment, metadata, can_set}` for the signed-in caller (401 otherwise) |
| HTTP | `PATCH /api/v1/flags/{key}` `{value, scope="user"}`: user scope for anyone signed in (own override); account scope for owner/operator/admin; node scope for operator/admin. 403 for a protected flag or a missing role, 404 for an undeclared flag, 400 for a bad value |

## 5. Audit

Every `flags.set` (any surface, refused or not) is recorded in the action audit
chain (`<state_dir>/audit/actions.jsonl`, read by `axi audit actions`) with the
caller as principal: `@cli:local` at the CLI, `@<sub>:<site>` on the web, the
dispatcher's actor on MCP.

## 6. Front end

A client fetches `GET /api/v1/flags` once per session and gates on `flags[key]`:
`off` hides the surface, `soon` shows it marked as coming, `on` enables it. The
client never computes precedence.
