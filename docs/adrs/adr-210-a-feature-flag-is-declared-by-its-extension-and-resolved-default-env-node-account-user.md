# ADR-210: A feature flag is declared by its extension and resolved default, env, node, account, user

**Status:** Accepted (2026-10-28)
**Related:** ADR-052 (extension-owned schemas), ADR-056 / ADR-072 (one skill, every surface),
ADR-164 (node functions, `axi features`), ADR-174 (Postgres is the runtime database),
ADR-185 (skill chaining declarations)
**Spec:** [`spec-flags.md`](../specs/spec-flags.md)

## Context

Axiom had no feature-flag system. An application built on it could not ship an
unfinished surface turned off, show a surface as "coming soon", or let one node,
one tenant or one person see something the rest do not. Each consumer improvised:
one web app read a JSON blob from an environment variable and injected it into the
page; another kept per-account and per-user overrides in JSON columns on its own
`accounts` and `users` tables.

The most complete of those is an earlier product's `FeatureFlagService`: a code
registry of flags, each with a flat or per-environment default and an optional
list of environments where it cannot be overridden; an environment variable per
flag; then an account override; then a user override. It had no audit on toggles,
no node scope, and flags were boolean.

Two existing Axiom things are close in name and are not this:

- **Node functions** (ADR-164, `axi features`, `infra/node_functions.py`): which
  functions a node runs. A deployment shape, confined to presentation in the CLI.
- **Settings** (`axi settings`): a node's preferences store. Not resolved per
  caller, not declared by extensions.

## Decision

### 1. Values are `on`, `off`, `soon`

A flag has three values. `soon` lets a surface be shown as coming without being
usable, which a boolean cannot say. Booleans and their usual spellings (`true`,
`yes`, `1`, `enabled` and their negatives) map to `on` and `off`.

### 2. An extension declares its flags

The registry is the union of what extensions declare, not a list in Axiom:

```toml
[[flags]]
key = "control"
description = "The Control verb in the rail"
default = "off"
default_by_env = { development = "on" }
protected_in = ["production"]
```

A package that is not an extension directory declares the same mappings through
the `axiom.flags` entry-point group. The AEOS manifest schema validates `[[flags]]`.
Keys are lowercase identifiers so `AXIOM_FLAG_<KEY>` is always spellable. The first
declaration of a key wins, in discovery order. A flag nobody declared resolves to
`off`.

### 3. Resolution, later wins

1. the registry default for this environment (`default_by_env`, else `default`)
2. the environment variable `AXIOM_FLAG_<KEY>`
3. the node override
4. the account override
5. the user override

The environment is `AXIOM_ENVIRONMENT`, else `ENVIRONMENT`, else `development`.
A flag `protected_in` the current environment ignores layers 3 to 5 there: the
deployment's own environment variable still applies; a stored override does not,
and setting one is refused. There is no role targeting and no percentage rollout.

Every resolution reports where each value came from (`default`, `env`, `node`,
`account`, `user`), so "why is this off for me" has an answer.

### 4. Overrides live in the flags extension's own table

The `flags` builtin owns one table, `flag_overrides` (ADR-052: its own schema,
reached only through `session_for("flags")`), with primary key
`(scope, scope_id, key)` and columns `value`, `updated_by`, `updated_at`. `scope` is
`node`, `account` or `user`. Overrides do not live in JSON columns on another
extension's tables. Postgres at runtime, SQLite in tests (ADR-174).

### 5. Three skills, every surface

`flags.list`, `flags.resolve` and `flags.set` are the only implementation. The CLI
(`axi flags`), MCP (`axiom_flags__*`) and HTTP (`/api/v1/flags`) reach them through
`invoke_capability`, so a value set on one surface resolves the same on the others.
The CLI noun is `flags`, never `features`.

Who may set which scope is decided at the surface:

- the node's CLI is its operator and sets any scope;
- over HTTP, anyone signed in reads their resolved map and sets their own user
  override; account scope needs the owner or an operator; node scope needs the
  operator role. The caller is the session: `sub` is the user, `site` the account.

### 6. Every write is audited

`flags.set` is a mutating skill, so every invocation, refused or not, lands in the
universal action audit (`axi audit actions`, the hash chain of the `audit` surface),
with the caller as principal. The stored row also carries `updated_by` and
`updated_at`.

## Consequences

- An application declares its verbs' flags in its own manifest; a deployment sets
  its values with `axi flags set KEY VALUE` (node scope) or an environment variable.
  For example, a first deploy that hides two verbs and marks a third as coming soon
  is four `axi flags set` calls, with no code change.
- A front end reads one resolved map (`GET /api/v1/flags`) and gates on it; it never
  computes precedence itself.
- Removing an override (`axi flags clear`) returns the flag to the layer below.
- Not carried over from the ancestor: its flag list (each product declares its own),
  the toggle verb (a toggle is not idempotent; set an explicit value), and the
  per-environment default treating a missing environment as `false` (here it falls
  back to the flat default).
