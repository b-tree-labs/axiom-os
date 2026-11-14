# Flags: feature flags over the one registry — Technical Specification

**Status:** Active
**Date:** 2026-10-28, revised 2026-11-10 and 2026-11-12 (cohorts, rollouts, kill switch, explain; every standard rollout mode, §8; two command nouns over one registry, §0.1)
**Decision:** [ADR-210](../adrs/adr-210-a-feature-flag-is-declared-by-its-extension-and-resolved-default-env-node-account-user.md), [ADR-254](../adrs/adr-254-one-switchboard-features-functions-and-flags-are-one-registry.md), ADR-257 D5 (exposure is decided in the record)
**Requirements:** PRD "The loop", §5.1, LOOP-F1 to F13 (F11 is §7.6); §5.1b, LOOP-F14 to F20, F22, F23 (§8; F21, F24, F25 are later phases)
**Code:** `src/axiom/extensions/builtins/features/` (declarations, resolution, skills, CLI, HTTP); `src/axiom/extensions/builtins/flags/` (the `flags` noun, its CLI and the `/api/v1/flags` route; storage)

---

## 0. Where a flag sits

A flag is one of the three kinds in the one registry (ADR-254): `extension` and
`function` rows are node scope; a `flag` has node, account and user scopes and the
value `soon`. Everything below is about the flag kind; the registry's node kinds
are documented in the `features` extension's README.

### 0.1 Two command nouns over one registry (owner decision, 2026-11-12)

ADR-254 put every kind under one noun, `features`. The owner split the command
noun on 2026-11-12 (the dated amendment at the top of ADR-254 and ADR-210); the
registry, the resolver, the extension-off rule, audit and storage do not change.

| Noun | Covers | CLI | MCP | HTTP |
|---|---|---|---|---|
| `features` (`neut features` in a product) | what a node runs: optional extensions, functions and commands, background agents, the role, the agent, maintenance and record-of-truth policies | `features [list] [--kind extension\|function]`, `enable`, `disable`, `set NAME on\|off\|clear` (node scope), `clear`, `role`, `agents`, `maintenance`, `record-of-truth` | `axiom_features__list\|set\|enable\|disable\|role` | `/api/v1/features`: the whole registry |
| `flags` | feature flags: every verb of §4 | `flags [list]`, `resolve`, `set`, `clear`, `explain`, `kill`, `cohort ...`, `rollout ...`, `stage ...`, `approve`, `changes`, `cancel`, `tick` | `axiom_flags__<verb>` for every verb offered to MCP | `/api/v1/flags`: the canonical flags endpoint |

- `features` lists no flag. Its output and its `features.list` value say how many
  flags are declared and point at `flags list` (`flags: {count, skill, command}`);
  asked for the flag kind (`--kind flag`), `features list` prints only that pointer.
- `features` sets no flag. A flag verb given to `features` (`resolve`, `explain`,
  `kill`, `cohort`, `rollout`, `stage`, `approve`, `changes`, `cancel`, `tick`)
  exits 2 with the `flags` command to run; `features set|enable|disable|clear` on
  a flag's name (and `features.set` on any surface) is refused with
  `flags set <name> <value>`. A name that is both a command and a flag is the
  command here, as `enable` always meant.
- `flags` is a visible noun (`tier = "core"`) with no deprecation note. The removal
  ADR-254 §5 scheduled for 0.71.0 is cancelled for the noun and the
  `axiom_flags__*` tools; the `flags/registry.py` and `flags/service.py` import
  shims may still go at 0.71.0.
- Deprecated for one release, removed at 0.71.0: the flag tools that existed only
  as `axiom_features__<verb>` keep that name (`resolve`, `explain`, `cohorts`,
  `cohort_preview`, `cohort_set`, `cohort_delete`, `rollout_preview`,
  `rollout_set`, `kill`, `cohort_import_preview`, `cohort_import`,
  `cohort_refresh`, `stage_plan_preview`, `stage_plan_set`, `stage_plan_halt`,
  `changes`, `change_cancel`; and `approve`, `rollout_tick` at the command line).
  Each is the same function as its `flags.*` skill, and its description starts
  "Deprecated: use flags.<verb>". `axiom_features__set` keeps its name for the
  node and refuses a flag.
- HTTP: both routes stay. `/api/v1/flags` gains `GET /flags/{key}/explain`,
  `POST /flags/{key}/kill` and `reason` / `expires_at` on `PATCH`; every field it
  returned before is unchanged. `/api/v1/features` returns the whole registry as
  before, and a `PATCH` of a flag there goes through `flags.set`.
- Stored change records keep the capability names they were written with
  (`features.cohort_set`, ...) and the scheduler still applies them; the change
  log maps both names to the same kind.
- No user-facing text says "switch" (the kill switch is the one exception: it is
  the industry's name for that verb).

## 1. Declaring a flag

In the owning extension's `axiom-extension.toml`:

```toml
[[flags]]
key = "control"                          # ^[a-z][a-z0-9_]{0,63}$
description = "The Control verb in the rail"
default = "off"                        # on | off | soon; true/false accepted
default_by_env = { development = "on" }
protected_in = ["production"]
bucket_by = "principal"                # the unit a percentage moves (§8.1): principal (default),
                                       # account, any catalogue attribute, or "cohort:a,b"
kind = "release"                       # release | operational | permission | experiment | kill_switch (§8.7)
remove_by = 2027-03-01                 # required for a release flag
requires = [{ flag = "editor", value = "on" }]   # prerequisites (§8.6)
approvals = { production = { approvals = 1, roles = ["release_manager"] } }   # §8.8
set_by = "person"                      # only a person changes it (below)
```

A multivariate flag (§8.3) declares `variants` (name = a string, a number or a
document), names one as its `default`, and optionally an `off_variant`.

Or, from a package that is not an extension directory, an entry point in the
`axiom.flags` group whose object is (or returns) a list of the same mappings.
A malformed declaration is logged and skipped; it never stops discovery. A flag
declared by an optional extension that is switched off on this node is still
in the registry (so `flags list` can list it and say why it is off).

## 2. Resolution

One order, most decisive first (LOOP-F7). The first step that decides gives the
value, and `sources[key]` (and `explain`'s `decided_by`) names that step.

| Order | Source | Where it comes from |
|---|---|---|
| 1 | `extension-off` | the owning extension is switched off on this node (ADR-254 §3): the value is `off`; the flag is reported as waiting on that extension (`waiting_on`), with what steps 2 to 9 give it (`staged`), which applies the moment the extension is on |
| 2 | `kill-switch` | the `rollouts` row for this flag and environment has `kill_switch` set (§7.4) |
| 3 | `prerequisite` | a flag this one `requires` does not resolve to a required value for this subject (§8.6): the flag serves its off value |
| 4 | `user` | `flag_overrides` row, scope `user`, id = the session's `sub` |
| 5 | `account` | scope `account`, id = the session's `site` (else `$AXIOM_SITE`) |
| 6 | `rollout` | the flag's rollout rules in this environment, the first rule that decides (§7.3); then the current stage of its stage plan (§8.4) |
| 7 | `node` | scope `node`, id = `AXIOM_NODE_NAME` or short hostname |
| 8 | `variant` | `[variant.<name>.flags]` of the running web app variant (`AXIOM_VARIANT`; `axiom.infra.variants`) |
| 9 | `env` | `AXIOM_FLAG_<KEY>` (unparseable values are ignored) |
| 10 | `default` | `default_by_env[env]`, else `default`, else `off` (a kill-switch kind: `on`) |

A multivariate flag's "off" (kill switch, unmet prerequisite, off extension) is its
`off_variant`, else its default variant.

An override (steps 3, 4, 6) carries an optional reason and expiry (LOOP-F10). An
expired override is ignored. In an environment listed in `protected_in`, only a
reasoned override applies, and `set` refuses an unreasoned one; the kill switch,
the rollout rules and the variant still apply there. Every ignored override is
reported by `explain` with its status (`expired`, `unreasoned_on_protected`,
`invalid`).

**A flag only a person changes (#1811).** `set_by = "person"` (the only value; anything
else is refused at load) holds every write to the flag, an override (`flags.set`), the
rollout (`flags.rollout_set`) and the kill switch (`flags.kill`), to a person
(`features/person_only.py`). The command line asks at the prompt and changes nothing
unless the person types `yes`; with no terminal it changes nothing. The web routes
(`PATCH /features/{key}`, `/flags/{key}`, `POST .../{key}/kill`) require a sign-in
within five minutes and answer 403 "sign in again" otherwise. An MCP tool, an agent tool
or an in-process caller is refused with the reason. A change a person scheduled is
applied later as theirs. `protected_in` is a different rule: it asks for a reason, not
for a person.

`env` is `AXIOM_ENVIRONMENT`, else `ENVIRONMENT`, else `development`.
An undeclared key resolves to `off`. The off set is read from the node
file on every resolution (`axiom.infra.optional_extensions.switched_off`), so a
long-lived server sees `features enable <extension>` without a restart.

**Variants (MVP-12-A4).** An isolated variant (the bare ones) stores and reads
its overrides under its own namespace (`variant:<name>/<id>`), so an override in
one variant never reaches another. The in-place variant (the site, which takes
the running install over) uses the install's own ids, so nothing set before it
is lost. `GET /api/v1/features` and `/api/v1/flags` name the answering variant
(`variant`).

**A node with no database (LOOP-F13).** A node whose role has no database function
(ADR-164 `needs_database`) resolves from the registry, the environment, the
variant and the extension-off rule only, with no cohorts, rollouts or kill
switches, and says so: `Resolution.targeting`, `flags.resolve`'s `targeting` and
`explain`'s `cohorts.why` carry the reason. A `set` of a flag override there is
refused with the reason, instead of a connection attempt. A record that cannot be
read (a database that is down, a table not yet migrated) resolves the same way and
says that instead.

## 3. Storage

Schema `flags` (ADR-052), table `flag_overrides`:

| Column | Type | Note |
|---|---|---|
| `scope` | varchar(16), PK | `node` \| `account` \| `user` (check constraint) |
| `scope_id` | varchar(255), PK | the node, account or user |
| `key` | varchar(64), PK | indexed; a flag key, or a recorded switch `extension:<name>` / `function:<name>` (ADR-254 §6) |
| `value` | varchar(8) | `on` \| `off` \| `soon` (check constraint) |
| `updated_by` | varchar(255) | the principal that set it |
| `updated_at` | timestamptz | |
| `reason` | text, null | why (LOOP-F10); a protected flag honours only a reasoned override |
| `expires_at` | timestamptz, null | when it stops applying (LOOP-F10) |
| `variant` | varchar(64), null | a multivariate flag's variant (LOOP-F17); `value` then holds `on` |

Table `cohorts` (LOOP-F1):

| Column | Type | Note |
|---|---|---|
| `key` | varchar(64), PK | `^[a-z][a-z0-9_-]{0,63}$` |
| `name`, `description` | varchar(255), text | for people |
| `rules` | jsonb, null | the rule (§7.1); null for a list-only cohort |
| `principals`, `accounts` | jsonb | the listed members (up to 100,000; LOOP-F16) |
| `source` | jsonb, null | the external group it follows, `{kind, ...}` (§8.2) |
| `refresh_every_s` | integer, null | how often the scheduler refreshes it from its source |
| `refreshed_at` | timestamptz, null | the last refresh, worked or not |
| `source_error` | text, null | why the last refresh failed (the members were kept) |
| `reason` | text, null | why it was last written |
| `created_by`, `created_at`, `updated_by`, `updated_at` | | |

Table `rollouts` (LOOP-F4, F6), one row per flag and environment:

| Column | Type | Note |
|---|---|---|
| `key`, `environment` | varchar(64), varchar(32), PK | |
| `rules` | jsonb, null | the ordered rules (§7.3); null when only a kill switch was ever set |
| `bucket_by` | varchar(16), null | `principal` \| `account` (check constraint); what the release before LOOP-F14 reads |
| `bucket_unit` | varchar(255), null | the unit (§8.1), any attribute or `cohort:a,b`; null reads `bucket_by`, then the flag's declared unit |
| `reason` | text, null | why the rules were last written |
| `kill_switch` | boolean | §7.4 |
| `kill_reason`, `kill_by`, `kill_at` | | who pulled it, when and why |
| `updated_by`, `updated_at` | | |

Table `stage_plans` (LOOP-F18), one row per flag and environment: `key`,
`environment` (PK); `plan` (jsonb, the normalised plan of §8.4); `status`
(`running` \| `halted` \| `done` \| `rolled_back`, check constraint); `stage` (the
current index, -1 once rolled back); `stage_entered_at`; `halted_reason`; `history`
(jsonb, every transition `{at, from, to, status, by, why}`); `reason`, `updated_by`,
`updated_at`.

Table `changes` (LOOP-F19, F23): a cohort, rollout or stage-plan change that waits on
approvals, on its time, or both. `id` (PK); `plan_hash` (indexed: the
`skill_chain.plan_digest` of the capability and its parameters, so an approval binds
exactly one call); `capability`, `params` (jsonb; an import stores the members, never
a file path), `subject`, `environment`, `author`, `reason`; `policy` (jsonb, the
policies that held it); `approvals` (jsonb, `[{by, roles, at, note}]`); `run_at`,
`timezone`; `status` (`awaiting_approval` \| `approved` \| `scheduled` \| `applied` \|
`failed` \| `cancelled`); `result` (jsonb); `created_at`, `updated_at`, `applied_at`.

Migrations: `flags/migrations/versions/0001_flag_overrides.py`, then
`0002_cohorts_rollouts_and_reasoned_overrides.py` (expand only: two tables and two
nullable columns), then `0003_rollout_modes.py` (expand only: six nullable columns
and the two tables above). Only `flags/store.py` opens a session on these tables
(`OverrideStore`, `TargetingStore`), and it caches nothing. The recorded switches share the table
without a schema change: flag keys never contain `:`, the listing shows only
declared flag keys, and the switchboard reads switch state from the node file
(the recorded row is the record; the file is the cache discovery reads; drift
is said, not fixed, and raises one alert: a `notifications.send` with a cause
`node.switch_drift`, once per drift, remembered in
`<state>/features/switch-drift-alerted.json`).

## 4. Skills and surfaces

| Skill | Risk | Inputs | Returns |
|---|---|---|---|
| `flags.list` | read | `prefix?`, `user?`, `account?` | `environment`, `flags[]` (each declared flag: `default`, `owner`, `protected`, `description`, `overrides`), `rows[]` (the registry's flag rows resolved for the caller: `value`, `source`, `owner`, `scopes`, `details.{default, protected, owner_off, waiting_on, staged_value, staged_source, overrides}`), `problem` |
| `flags.resolve` | read | `key?`, `node?`, `account?`, `user?`, `roles?`, `locale?`, `principal_kind?` | environment, `for`, `flags` (key → value), `sources` (key → the step that decided it, §2), `waiting_on` (key → the off extension it waits on), `staged` (key → `{value, source}` the layers give it), `targeting` (`""`, or why cohorts were not read) |
| `flags.set` | reversible-write | `key`, `value` (`on`/`off`/`soon`/`clear`), `scope` (default `node`), `scope_id?`, `reason?`, `expires_at?` | the row written (with `reason`, `expires_at`), or `cleared`; `waiting_on` names the off extension a stored value waits on (a flag of an off extension is stored, not refused) |
| `flags.explain` | read | `key`, the subject inputs of `flags.resolve` | §7.5 |
| `flags.cohorts` | read | `environment?` | the stored cohorts (each with `used_by`), this environment's rollouts and kill switches |
| `flags.cohort_preview` | read | `key`, `name?`, `description?`, `rules?`, `principals?`, `accounts?`, `subjects?` | `before`, `after`, `members {count, sample, unknown}`, `population {size, source}`, `used_by`, `applied: false` |
| `flags.cohort_set` | destructive | the preview's inputs and `reason` | `before`, `after`, `applied: true` |
| `flags.cohort_delete` | destructive | `key`, `reason` | refused while a cohort or rollout names it |
| `flags.rollout_preview` | read | `key`, `rules`, `environment?`, `bucket_by?`, `subjects?` | `before`, `after`, `changes {count, sample}`, `per_rule [{rule_index, admitted}]`, `population`, `applied: false` |
| `flags.rollout_set` | destructive | the preview's inputs and `reason` | `before`, `after`, `kill_switch`, `applied: true` |
| `flags.kill` | reversible-write | `key`, `reason` (to engage), `engaged?` (default true), `environment?` | the kill switch state and the flag's value now |
| `flags.cohort_import_preview` | read | `key`, `file` or `members`, `kind?` (`principal`), `mode?` (`replace` \| `add`), `at?`, `timezone?` | `before`, `after` (with `size`), `added`, `removed`, `scheduled_for` |
| `flags.cohort_import` | destructive | the preview's inputs and `reason` | `applied: true`, or held / scheduled (§8.5, §8.8) |
| `flags.cohort_refresh` | reversible-write | `key` | `{size, before, error}`; refused (members kept) when the source fails |
| `flags.stage_plan_preview` | read | `key`, `plan`, `environment?`, `stage?`, `subjects?`, `at?`, `timezone?` | each stage with its `label`, `soak_s`, `admitted` and `sample`; `gates`, `on_fail`, `population` |
| `flags.stage_plan_set` | destructive | the preview's inputs and `reason` | the plan started at `stage`, or held / scheduled |
| `flags.stage_plan_halt` | reversible-write | `key`, `reason`, `mode?` (`halt` \| `rollback`), `environment?` | the plan's new status; no plan, like the kill switch |
| `flags.approve` | reversible-write, CLI only | `id`, `reason?` | the change with its approvals and what it still needs |
| `flags.changes` | read | `status?` | the waiting and past changes, newest first |
| `flags.change_cancel` | reversible-write | `id`, `reason` | the cancelled change |
| `flags.rollout_tick` | reversible-write, CLI only | `now?` | `{applied, failed, advanced, done, halted, rolled_back, refreshed, refresh_failed}`; the scheduler runs it every minute |
| `features.<verb>` for every verb above but `list` and `set` | as above | as above | deprecated aliases, removed at 0.71.0 (§0.1) |

| Surface | Form |
|---|---|
| CLI | `flags [list] [--prefix] [--user] [--account]`, `flags resolve [KEY] [--node] [--account] [--user] [--role]`, `flags set KEY VALUE [--scope] [--id] [--reason] [--expires]`, `flags clear KEY [--scope] [--id]`, `flags explain KEY [--user] [--account] [--node] [--role ...] [--locale] [--principal-kind]`, `flags cohort list|show KEY|set KEY [--name] [--description] [--rule JSON] [--principal ...] [--account ...] [--reason] [--apply]|delete KEY [--reason] [--apply]`, `flags rollout show KEY [--env]|set KEY --rules JSON [--bucket-by] [--env] [--reason] [--apply] [--at T --timezone Z]`, `flags kill KEY --reason R [--env]` / `--release`, `flags cohort import KEY --file F [--kind] [--mode] [--reason] [--apply]`, `flags cohort refresh KEY`, `flags stage show KEY|set KEY --plan JSON [--stage N] [--apply]|halt KEY --reason R [--rollback]`, `flags approve ID`, `flags changes [--status]`, `flags cancel ID --reason R`, `flags tick [--now]`, `--json` |
| MCP | `axiom_flags__list`, `axiom_flags__resolve`, `axiom_flags__set`, `axiom_flags__explain`, `axiom_flags__cohorts`, `axiom_flags__cohort_preview`, `axiom_flags__cohort_set`, `axiom_flags__cohort_delete`, `axiom_flags__rollout_preview`, `axiom_flags__rollout_set`, `axiom_flags__kill`, `axiom_flags__cohort_import_preview`, `axiom_flags__cohort_import`, `axiom_flags__cohort_refresh`, `axiom_flags__stage_plan_preview`, `axiom_flags__stage_plan_set`, `axiom_flags__stage_plan_halt`, `axiom_flags__changes`, `axiom_flags__change_cancel` (never `approve` or `rollout_tick`); the same names as `axiom_features__<verb>`, deprecated, until 0.71.0 (§0.1) |
| HTTP | `GET /api/v1/flags`: `{flags, values, sources, waiting_on, environment, metadata, can_set, variant}` for the signed-in caller (401 otherwise), the shape appkit's `FlagProvider` reads. `PATCH /api/v1/flags/{key}` `{value, scope="user", reason?, expires_at?}`. `GET /api/v1/flags/{key}/explain[?user=&account=]`: §7.5 for the caller; naming somebody else needs the site-admin role. `POST /api/v1/flags/{key}/kill` `{reason, engaged=true, environment?}`: site-admin role. Cohort and rollout edits are not on the web (they are destructive, planned on the CLI or MCP) |
| HTTP (whole registry) | `GET /api/v1/features`: everything `GET /api/v1/flags` returns plus `{switches, kinds, scopes, node, drift}` (`switches`: every row of the three kinds). `PATCH /api/v1/features/{key}` `{value, scope="user", kind?, reason?, expires_at?}`: a flag goes through `flags.set` with the scope rules below; an extension or function is node scope and needs the site-admin role. `/api/v1/features/{key}/explain` and `/kill` answer as the `/flags` ones do |

Scope rules on the web: user scope for anyone signed in (own override);
account scope for owner/site-admin/admin; node scope for site-admin/admin (a grant stored as `operator` counts as `site-admin`, ADR-261). 403 for
an unreasoned override of a protected flag or a missing role, 404 for an undeclared name, 409 for a
command of an extension that is off here, 400 for a bad value or a scope the
kind does not have. A flag whose extension is off here is stored, not refused,
and the answer names what it waits on (`set.waiting_on`, `waiting_on`).

## 5. Audit

Every write (any surface, refused or not) is recorded in the action audit chain
(`<state_dir>/audit/actions.jsonl`, read by `audit actions`) with the caller as
principal: `@cli:local` at the CLI, `@<sub>:<site>` on the web, the
dispatcher's actor on MCP. A write that ran notes its change (`kind` `flag`,
`extension`, `function`, `cohort`, `rollout`, `kill-switch`, `stage-plan`,
`scheduled-change` or `approval`; subject; old; new),
which the change log shows and filters by kind. A cohort's old and new are its
definition; a rollout's are `{rules, bucket_by}` with the subject `<key> (<environment>)`;
a kill switch's are `off`/`on`. A member list longer than 50 is recorded as
`{count, sha256, sample}`, never in full. The scheduler's automatic transitions
write records of their own: `features.change_apply` (a scheduled change, carrying
that change's note), `features.stage_advance`, `features.stage_halt` and
`features.stage_rollback` (kind `stage-plan`, old and new `{stage, status}`). The
flag verbs record under their `flags.*` names (a deprecated `features.<verb>` call
under the name it was called by); the node verbs under `features.set` /
`features.enable` / `features.disable`.

## 6. Front end

A client fetches `GET /api/v1/flags` once per session (appkit's `FlagProvider`;
`/api/v1/features` answers the same `flags` and `sources`) and
gates on `flags[key]`: `off` hides the surface, `soon` shows it marked as
coming, `on` enables it. A multivariate flag's `flags[key]` is the variant's name
and `values[key]` its value (a string, a number or a document), both resolved on
the server. A `sources[key]` of `extension-off` means the owning
extension is off on the node, and `waiting_on[key]` names it: a settings page
shows such a flag as "waiting on extension <name>" and may still set it; the
client never computes precedence. The
`/api/v1/features` body's `switches` carries every kind for one Settings page.


## 7. Cohorts, rollouts, kill switch and explain (ADR-257 D5)

Code: `features/targeting.py` (the pure rules: grammar, three-valued evaluation,
buckets, windows, override reading), `features/attributes.py` (the catalogue),
`features/resolver.py` (`CohortMatcher`, the order of §2, `explain`),
`features/skills/_loop.py` (the verbs), storage in `flags/store.py`. The design is
ported from a rule engine proven in a product built on this platform.

### 7.1 Cohorts (LOOP-F1, F2)

A cohort is a named, stored set. A subject is a member when its principal or its
account is listed, or when the cohort's rule is true. A rule is JSON:

```json
{"and": [{"attr": "role", "op": "contains", "value": "site-admin"},
         {"not": {"in_cohort": "trial_accounts"}}]}
```

The `role` attribute holds the caller's roles in their current ids plus any retired id
that names one of them, so a rule stored before ADR-261 that says `operator` still
matches a site admin.

`and`/`all`, `or`/`any`, `not`; an attribute test with `eq`, `neq`, `in`, `not_in`,
`contains`, `gt`, `gte`, `lt`, `lte`, `exists`; `in_cohort` (alias `cohort`) for
membership of another cohort. Strings compare without case; a list-valued attribute
matches `eq`/`in`/`contains` when any item does. At save time a rule is refused when
it nests deeper than 8, has more than 200 nodes, names an unknown attribute, operator
or cohort, or would form a cycle (the refusal names the path, `a -> b -> a`), or when
the cohort chain nests deeper than 8.

**Three-valued logic.** An attribute with no resolver, a resolver that fails, or a
resolver with no value for this subject is UNKNOWN. `and` is false if any branch is
false, `or` true if any is true, `not` keeps UNKNOWN. A final UNKNOWN is not a
member, so nobody is admitted on a fact the platform does not have, even through
`not`. `exists` is false for a missing value (and UNKNOWN with no resolver).

### 7.2 The attribute catalogue (LOOP-F3)

| Attribute | Resolved from |
|---|---|
| `principal` | the subject's principal (user id) |
| `principal_kind` | `person` on the web (a session); otherwise what the caller passes; else UNKNOWN |
| `role` | the session's roles on the web; otherwise what the caller passes; else UNKNOWN |
| `account` | the subject's account |
| `account_kind` | no platform resolver: UNKNOWN until an extension declares one |
| `site` | `$AXIOM_SITE` |
| `node` | the node the flag is resolved on |
| `node_role` | this node's role (ADR-164), UNKNOWN when it has none |
| `environment` | the deployment environment |
| `locale` | the browser's language on the web; otherwise what the caller passes; else UNKNOWN |
| `timezone`, `country`, `region`, `site_*`, `node_*`, `local_timezone`, `local_hour`, `day_of_week` | §8.1 (LOOP-F15) |

An extension adds attributes in its manifest:

```toml
[[flag_attributes]]
name = "plan_tier"
description = "The account's plan"
resolver = "my_ext.attributes:plan_tier"   # callable(subject) -> value | None
```

A platform attribute's name is never taken over (the declaration is logged and
ignored); a malformed declaration is skipped. A resolver is imported and called
lazily, once per request.

### 7.3 Rollouts and percentages (LOOP-F4, F5)

A rollout is one flag's ordered list of rules in one environment:

```json
[{"cohort": "staff", "value": "on"},
 {"match": {"attr": "locale", "op": "in", "value": ["fr-CA", "fr-FR"]}, "value": "on", "percentage": 25},
 {"value": "soon", "active_from": "2026-12-01T00:00:00Z"}]
```

Each rule names a `cohort` or an inline `match` (never both; neither means
everyone), a `value` (`on`, `off`, `soon`, or a declared variant) or a `split`
(§8.3, never both), and optionally a `percentage` (default 100), its own
`bucket_by` unit (§8.1), a window `[active_from, active_until)` and a `note`. Rules are tried in order;
a rule decides when its target is true and the subject's bucket is under the
percentage. A subject that is not admitted falls through to the next rule, then to
step 6 of §2. At most 50 rules.

The bucket is `sha256("<flag key>:<subject>")`, the first 8 bytes mod 10000 over 100,
in [0, 100): the same on every node and in every session, and independent between
flags. The subject is the **principal** by default, or the **account** when the
flag declares `bucket_by = "account"` or the rollout row sets it (so every principal
of one account shares one assignment). Which should be the default is the PRD's open
question 2; the principal is the default until the owner decides. A percentage rule
with no subject to bucket cannot admit, and `explain` reports it
(`no_subject_to_bucket`). Since LOOP-F14 the unit may be any attribute or a list of
cohorts (§8.1).

### 7.4 The kill switch (LOOP-F6)

One per flag and environment, on the `rollouts` row. Engaged, the flag resolves
`off` for everyone, above every override and rollout (only `extension-off` sits
above it), from the next request: every resolution reads the record afresh, with no
process cache; it memoises per request only. `flags.kill` is `reversible-write`
and needs no plan, while cohort and rollout writes are `destructive` and run off the
command line only with a person's approved plan: the kill switch needs the lower
permission, so an incident is stopped fast. On the web it needs the site-admin role.
Engaging needs a reason.

### 7.5 Explain (LOOP-F8)

`flags.explain` (CLI `flags explain`, MCP `axiom_flags__explain`, HTTP
`GET /api/v1/features/{key}/explain`) returns: `flag`, `environment`, `subject`,
`value`, `decided_by` (a source of §2), `detail` (the deciding step's specifics:
an override's `reason`, `expires_at`, `set_by`; a rule's `rule_index`, `cohort` or
`match`, `cohorts_matched`, `percentage`, `bucket`; the kill switch's `reason`; the
environment variable), `waiting_on` and `staged`, `order`, `protected`,
`kill_switch`, `rules`, `bucket_by`, `bucket`, `evaluated` (each rule tried, with
`member` and every cohort consulted, nested ones included, as true, false or null),
`ignored` (each override or rule present that did not apply, with its status) and
`unknown_attributes`, and `cohorts {available, why}`.

### 7.6 Previewed, planned and audited writes (LOOP-F9); later phases

A cohort or rollout write is previewed first: `before`, `after`, and who it affects
(counts and a sample of up to 10). Counts are over the subjects this node knows of:
principals and accounts with a stored override, members listed in any cohort, and
any the caller names in `subjects`; the preview says so (`population.source`). The
write itself is a destructive skill: on MCP and the web it opens a plan and applies
only with the `plan_hash` a person approved (ADR-185); at the command line the
person at the terminal is the approval, and `--apply` is required (without it the
command previews). Every write needs a reason and notes its before and after (§5).
A cohort that a cohort or rollout names cannot be deleted.

**Owners, review dates and the stale report (LOOP-F11).** A flag declares `owner` and
`review_by` (`FlagDef.owned_by`, `FlagDef.review_by`). A flag's owner is the maintainer of the
extension that declares it, not a site's site admins (owner's decision, 2026-11-12). Axiom source
names no person, so every built-in flag says `owner = "@maintainers"` and the node setting
`flags.maintainers` (a list of `@principal` handles; role handles such as `@site-admin` are
ignored) says who that is.

- **The report.** `flags stale`, `axiom_flags__stale` and `GET /api/v1/flags/stale` list the flags
  that are work to remove: `on_past_review` (every value the flag can serve is `on` and today is
  past `review_by`) and `off_unreferenced` (off everywhere and no code refers to it). A kill
  switch or permission flag is never stale. It is read-only and visible to site admins and to
  maintainers; anyone else is refused. With `flags.maintainers` unset the report carries
  `maintainers: []` and `note: "no maintainer configured..."`.
- **The notice.** The weekly schedule (`flags_stale_weekly`, `flags.stale_notify`, behind the
  `flag_review_notices` flag, default off) sends each configured maintainer one grouped message.
  It is never sent to a site admin. With no maintainer configured nothing is sent. Site admins keep
  rollouts, stops and feedback asks; lifecycle notices are not theirs.
- **Built-in flags.** `library_labels` and `approvals_signed` are `permission`;
  `feedback_auto_stop` is `operational`; the other nine are `release` with `review_by`
  2027-02-10. All twelve are owned by `@maintainers`.

The web Settings page for cohorts and rollouts is appkit's.

## 8. Every standard rollout mode (LOOP-F14 to F20, F22, F23)

The owner's direction (PRD §5.1b): rollouts are managed mostly at cohort scale, from
tens of agents to whole geographies or time zones; small hand-picked sets are the
rarer mode. Code: `targeting.py` (units, splits, durations, local times, stage
rules), `attributes.py` (geography and time), `cohort_sources.py`, `progressive.py`,
`approvals.py`, `skills/_modes.py`; storage in `flags/store.py`.

### 8.1 The unit, geography and time (LOOP-F14, F15)

A percentage or a split buckets a **unit**: `principal` (the default), `account`, any
attribute in the catalogue (`node`, `site`, `country`, `local_timezone`, an
extension's own), or `cohort:<a>,<b>,...` (each listed cohort moves as one; a subject
belongs to the first listed cohort it is a member of). Everyone who shares the unit's
value lands in the same bucket, so a rollout moves whole sites, regions or groups of
agents at a time. The unit is set on the rule, else on the rollout row, else declared
by the flag. The bucket is `sha256("<flag>:<id>")` as in §7.3, where the id is the
bare principal or account (so nothing assigned before F14 moves) and `<unit>=<value>`
for every other unit. **A subject whose unit is UNKNOWN is never admitted by a
percentage or a split** (`explain`: `unit_unknown` with the unit). A unit that is not
in the catalogue, or names a cohort that does not exist, is refused at save time.

| Attribute | Resolved from |
|---|---|
| `timezone`, `country`, `region` | the principal's: on the web the session's OIDC `zoneinfo` and `address.country` / `address.region` claims; at the CLI or MCP what the caller states; else UNKNOWN |
| `site_timezone` | `$AXIOM_SITE_TZ`, else the `site.timezone` setting |
| `site_country`, `site_region` | the `site.country` / `site.region` settings (declared, empty by default) |
| `node_timezone`, `node_country`, `node_region` | this node's file, `[settings]` `"node.timezone"` / `"node.country"` / `"node.region"`; UNKNOWN for a subject on another node |
| `local_timezone` | the principal's zone, else the node's, else the site's |
| `local_hour` | local time of day in decimal hours (09:30 is 9.5), read at the resolution's instant |
| `day_of_week` | `mon` to `sun`, local |

Zones are read through the tz database (`zoneinfo`), so daylight saving is right; a
name it does not know is UNKNOWN, never UTC. Nothing is invented: an unset key is
UNKNOWN. "Only during local working hours" is
`{"and": [{"attr": "local_hour", "op": "gte", "value": 9}, {"attr": "local_hour", "op": "lt", "value": 17}, {"attr": "day_of_week", "op": "in", "value": ["mon", "tue", "wed", "thu", "fri"]}]}`.
`explain` takes `at` to answer for another instant.

### 8.2 Large explicit sets and external groups (LOOP-F16)

A cohort lists up to 100,000 members in the record, never in a flag, and reports
`size`. `flags.cohort_import` replaces or adds to the list from a file (one per
line, a CSV's first column, or a JSON list; a header naming the column, blank lines
and `#` comments are skipped) or from a list; its preview counts what is listed,
added and removed. Approvals and schedules bind the members themselves (their
digest), never the file path. Membership is looked up as a set built once a request.

A cohort may instead follow an external group: `source = {kind, ...}` and
`refresh_every` (`30m`, `1h`, `1d`). The members are taken from the source when the
cohort is set, then by the scheduler whenever it is due, or now with
`flags.cohort_refresh`. A refresh that fails keeps the last members and records
`source_error`. Sources are pluggable (`cohort_sources.register_source(kind, fn)`);
the platform ships `directory`: a group of the identity provider the node's directory
seam is configured for (ADR-103, `AXIOM_DIRECTORY_PROVIDER=local|entra`), which must
be able to list members. A cohort that follows a source cannot be imported into.

### 8.3 Multivariate flags (LOOP-F17)

A flag with `variants` resolves to one of their names; `values[key]` carries the
variant's value. A rule serves one variant (`value`) or splits its unit across
variants by weight: `"split": [{"variant": "wide", "weight": 30}, ...]` (weights
above 0, normalised; up to 20 parts). The split position is
`sha256("<flag>#split:<id>")`, salted apart from the admission bucket, so whether a
subject is admitted and which variant it gets are independent, and stable. A split
works on an on/off/soon flag too. Overrides take a variant name. A rule naming an
undeclared variant is refused.

### 8.4 Progressive rollout (LOOP-F18)

A stage plan per flag and environment: `stages` (rising percentages, rings of cohorts
that are cumulative, or `{"everyone": true}`), a minimum `soak` (per stage or for
all; default `1h`), what it serves (`value` or `split`), an optional `bucket_by`,
`gates`, and `on_fail` (`halt`, the default, or `rollback`). The current stage stands
for rollout rules evaluated after the flag's own rules (§2 step 6); `explain` reports
`stage {index, of, status}`.

The concepts are ADR-197's switch runner. Each gate is a declared probe
(`axiom.infra.switchplan.Probe`, kind `callable`) run by `switchplan.evaluate`, so a
gate that cannot be evaluated **fails**. The platform scheduler runs
`flags.rollout_tick` every minute (a hosted `[[extension.schedule]]` in the
features manifest; no loop of our own). On each tick a running plan's gates are
read; any failure halts it where it stands, or rolls it back (status `rolled_back`,
stage -1: the plan serves nobody and the flag falls to the next step of §2); when
they hold and the stage has soaked, it moves to the next stage, and after the last
stage soaks it is `done`. Every automatic transition writes one audit record (§5)
and raises one notification: an advance or finish is announced to `@operator`; a
halt or roll-back is an incident with a cause (`features.rollout_halted`, ADR-249),
owned by RIVET, once, since a halted plan is not ticked again. A person halts or
rolls back at once with `flags.stage_plan_halt` (low permission, like the kill
switch) and resumes with `flags.stage_plan_set ... stage=N`.

Gate kinds are pluggable (`progressive.register_gate_kind`). Built in: `incidents`
(open incidents naming `flag:<key>`, of declared `kinds` if any, at most `max`,
default 0; the plan's own halt incidents are not counted) and `metric` (a declared
**read** capability, such as a gold read, invoked through the dispatch chokepoint;
the number at `path` compared with `op` `lte|lt|gte|gt|eq` and `value`; a capability
that writes is refused when the plan is saved). `feedback` is the seam for LOOP-B's
feedback reports: until a source registers it, a plan naming it is refused.

### 8.5 Scheduled changes (LOOP-F19)

Every cohort, rollout, import and stage-plan write takes `at` and `timezone` (an IANA
zone; required when `at` has no UTC offset). It is previewed (`scheduled_for`, with
the local and UTC times) and approved exactly like an immediate change: the same
destructive plan gate off the command line, the same approvals (§8.8). Then it is
stored in `changes` as `scheduled`, and the tick applies it when due, as its author,
writing the change's own audit record. A wall-clock time the zone skips (clocks jump
forward) or passes twice (they fall back) is refused, not guessed; so is a time in
the past. A change that no longer applies when its time comes (a cohort it names was
deleted) is marked `failed` with the reason and raises one incident.
`flags.change_cancel` cancels a waiting change.

### 8.6 Prerequisites (LOOP-F20)

`requires = [{flag, value}]` (a value or a list of values): the flag serves its off
value for a subject unless every required flag resolves to an allowed value for the
same subject (§2 step 3). Each flag is decided once a request, so a chain costs one
evaluation per flag. A cycle is refused when the registry is built (every flag on it
is dropped, and the log names the path `a -> b -> c -> a`); a flag that requires an
undeclared or refused flag stays, and its prerequisite is never met. `explain`'s
`prerequisites` lists each prerequisite with `required`, `value`, `decided_by`,
`met`, `declared` and its own `prerequisites`, to the end of the chain.

### 8.7 Kinds (LOOP-F22)

`kind`: `release` (must declare `remove_by`; a declaration without it is skipped and
logged), `operational`, `permission`, `experiment` (F21's experiment machinery is a
later phase; the kind is declared now), `kill_switch` (defaults `on` when no default
is given). A kill switch or permission flag is exempt from staleness. A flag declared
before kinds existed has none: it is listed as unclassified (`details.flag_kind`
null) and resolves exactly as before. LOOP-F11's stale report is §7.6; a release flag's
`removal_due(today)` is the separate removal-date check.

### 8.8 Approvals by environment (LOOP-F23)

A policy is `{approvals, roles, two_person}`: how many people other than the author
must approve (`two_person = true` is at least one), and which roles they hold (any
when none is named). A flag declares one per environment in its manifest; otherwise
the environment's setting applies: `features.approvals.<env>.approvals`,
`.roles`, `.two_person`. A rollout or stage-plan write answers to its flag's policy
in its environment; a cohort write (set, delete, import) answers to every policy of
every flag and environment whose rollout or stage plan reads the cohort, directly or
through another cohort.

A held write is refused with `unmet: [{id: approvals, why, fix}]` and recorded in
`changes` (`awaiting_approval`); another person runs `flags approve <id>` (offered
only at the command line, never to an AI client; refused for the author and for a
platform agent on the roster); the same call again then applies, once. An approver's
roles come from the platform's one permission model: the directory's groups for that
principal mapped to roles by the deployment's group-to-role map (ADR-103), the same
facts the authorization layer reads. The kill switch and a stage plan's halt are
never held.

