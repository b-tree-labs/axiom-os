# ADR-208: Conforming utilities are governed catalog objects

**Status:** Proposed  •  **Date:** 2026-10-28  •  **Deciders:** platform owner

**Related:** ADR-128 (tier boundaries), ADR-131 (there is always a medallion),
ADR-151 (roles), ADR-157 (serving budgets), ADR-185 (verb chaining), ADR-193
(incremental conform), ADR-203 (a kit is promoted as data), ADR-205 (grants and
sharing programs, proposed), ADR-206 (reading identity and scoped rebuild), ADR-056
and ADR-072 (one skill, every surface). Depends on the site generalization ADR
(forthcoming), which makes every installation a site and lets a site be shared,
organization-private or public; D9's levels and D10's licence default are stated in
terms of that site. Evidence:
[the inventory](../working/conforming-utilities-inventory-2026-10-28.md). Background:
[the Databricks mapping](../working/conforming-utilities-databricks-mapping-2026-10-28.md).
The declared expectation being built on `feat/conform-expectations` is the first
utility this ADR describes; this ADR names its kind and contract and leaves its
implementation to that work.

## Context

The data kit (ADR-203) lets anyone contribute conform code, gold SQL, verbs and charts,
prove them on their own machine, and promote them. That is the point of it, and it has
a cost the owner named on 2026-10-28: with many contributors, each writing the small
steps they need, the platform fills with slightly different ways of doing the same
thing, and nobody writing new conform code or a new gold verb can see what already
exists.

The inventory shows that this has already happened, before the kit has many users:

- about 16 functions in the platform turn an ISO string into an aware UTC instant,
  9 more attach a timezone, and the domain consumer and site kits add at least 10
  more. **They disagree:** the deposit path refuses a naive timestamp, most others
  assume UTC, one leaves it naive, and one site script stamps a local-time logger's
  values as UTC;
- there is no unit converter anywhere, and unit spelling, inference or the
  "undeclared unit" policy is written in at least 9 places;
- there are three quality vocabularies and five sets of "modelled" source classes,
  with two spellings of the same word;
- three site kits each hand-roll a reader of the same shape and copy the same gold
  view and verb.

Each copy was a reasonable local decision. What is missing is a place where a
contributor can find a utility, a rule that a contribution says what it is, and a
check that says "this exists already" at the moment someone is about to add it.

Two constraints shape the answer. ADR-203 decision 6 keeps contributor Python out of
every platform process, so a promotion of new code waits for the isolated conform
worker. And colleagues are being onboarded now, through a guide whose Part 2 teaches
the kit, so the first useful step has to work with today's verbs.

## Decision

**A conforming utility is a named, versioned, typed object in a catalog. Contributors
find utilities, compose pipelines from them, and contribute new ones through the kit;
the kit's gate compares every new utility with the catalog before it can be promoted.**

### D1. The unit of contribution is a utility with a manifest

A utility is one of six kinds:

| Kind | What it is | Reads | Writes |
|---|---|---|---|
| `function` | A pure helper over values: parse an instant, spell a unit, convert a quantity | values | values |
| `reader` | Turns one bronze shape (a record, a deposit file) into records | bronze | records |
| `normalizer` | Turns records into silver rows | records | silver |
| `expectation` | A named constraint on silver rows, with an action (D4) | silver rows | quality, reason |
| `derivation` | A new silver channel from existing ones | silver | silver |
| `gold` | A gold view template, or a verb over gold | silver, gold | gold |

Each utility has a manifest, `utility.toml`, beside its code or declaration:

```toml
[utility]
name        = "core.time.parse_instant"   # D2
version     = "2.0.0"                      # semantic version; D5 says when to bump
kind        = "function"
summary     = "Parse an ISO-8601 instant into an aware UTC datetime."
owner       = "@data-platform:axiom"       # a principal or a directory group
contributors = ["@ana.lee", "@sam.ortiz:example-loop"]
entry       = "axiom.utilities.time:parse_instant"   # code; or `sql = "view.sql"`
tier_writes = "none"                       # none | silver | gold (ADR-128)
export_class = "open"                      # open | controlled (D6)
tags        = ["time", "iso8601"]
supersedes  = []                           # names this utility replaced (D3)

[inputs]                                   # typed signature
value  = { type = "str" }
[params]
assume = { type = "str", enum = ["refuse", "utc"], default = "refuse",
           doc = "What to do with a naive timestamp." }
[outputs]
instant = { type = "datetime", tz = "UTC" }

[example]                                  # shown by `utils show`, run as a test
call   = { value = "2026-01-02 03:04:05+00" }
result = { instant = "2026-01-02T03:04:05+00:00" }

[tests]
golden = "tests/golden/parse_instant.jsonl"  # input and expected output, one per line
probe_set = "core.time"                    # the shared probe corpus for its area (D3)
```

A normalizer's `[outputs]` is the silver row contract (`feed`, `channel`, `ts`,
`value`, `unit`, `source_class`, optional uncertainty terms). A gold utility's
`[outputs]` is its column list. A reader names the bronze `schema_ref` values it
accepts.

Today's kit files become utilities without being rewritten: `kit-check` synthesizes a
manifest for a `conform/*.py` normalizer from its module (its `SCHEMA_REF`, `UNITS`
and docstring), for a `silver/derived.toml` entry, and for a gold object and verb pair.
A synthesized manifest is marked so, and `kit-check` asks for the fields it could not
infer (owner, summary, golden tests) before the utility can be promoted.

### D2. One catalog, with a three-level name

A utility's name is `<scope>.<area>.<name>`, the shape of a Unity Catalog name:

- **scope** is who governs it:
  - `core`: the platform (Axiom), released with it;
  - a domain pack's name: a consumer layer's shared utilities, released with that pack;
  - a site id: utilities promoted at one site (`example_loop.derive.delta_t`);
  - `kit`: a contributor's sandbox, visible only to them until promoted.
- **area** is a controlled list: `time`, `units`, `vocab`, `quality`, `readers`,
  `normalize`, `channels`, `derive`, `gold`. A new area is a platform change.
- **name** is `verb_noun` in lower snake case (`parse_instant`, `canonical_unit`,
  `expect_range`, `delta_between`, `hourly_summary`).

A version pins with `@`: `core.time.parse_instant@2`. An unpinned reference in a
pipeline is refused (D4).

The catalog is a table on each node (`utility_catalog.utilities`, expand-only, beside
the kit registry) plus an index of what the installed packages provide. `core` and
domain entries are indexed from the installed distributions (the same entry points
`register_discovered` honours today, plus their manifests). Site entries are written
by approval, exactly as ADR-203 writes verbs. Kit entries live in the contributor's
sandbox. A federated node lists another node's site utilities by reference (ADR-131
§7 and §10) and never copies their code by listing them.

**Discovery is one skill set on every surface** (ADR-056, ADR-072):

```text
axi data utils list [--scope S] [--kind K] [--area A] [--mine] [--usable]
axi data utils search "naive timestamp"         # text over name, summary, tags, example
axi data utils search --like conform/my_reader.py:parse_time   # behaviour search (D3)
axi data utils show core.time.parse_instant     # manifest, example, versions, users
axi data utils try core.time.parse_instant --value "2026-01-02 03:04:05"
axi data utils use core.quality.expect_range --into . --param channel=temp.* --param max=400
axi data utils diff core.time.parse_instant kit.time.my_parse   # behaviour difference
```

The same skills are MCP tools (`data.utils_list`, `data.utils_show`,
`data.utils_search`, `data.utils_use`), a catalog page in the web app, and chat tools.
`list --usable` is "what is available to me": the utility versions the person may
`discover`, answered by one authorization `ListObjects` call (D9), which covers
`core` and installed domain utilities, the person's sites, the programs their sites
share with, their kits and their own private utilities. `show` reports
who uses a utility (the pipelines that name it, per site the reader may see) so a
contributor can judge what is established.

### D3. Duplication is caught when a utility is contributed

`kit-check` compares every new or changed utility in the kit with the catalog, in
three passes, cheapest first:

1. **Signature.** Same kind, compatible inputs and outputs, same area. This shortlists
   candidates; it decides nothing by itself.
2. **Behaviour.** The candidate and each shortlisted utility run on the area's probe
   corpus (`core.time` has naive, `Z`, offset, space-separated, fractional and
   out-of-range instants; `core.units` has spellings and headers; a reader runs on the
   kit's own samples). Each output is hashed into a behaviour fingerprint. Equal on
   every probe is a **duplicate**. Equal on most, different on some, is a
   **near-duplicate**, and the finding names the probes that differ, since that
   difference is usually either a bug in the new code or an undocumented choice in the
   old.
3. **Structure.** A normalized syntax-tree hash (names, docstrings and formatting
   removed) and token shingles catch copied code that the behaviour pass could not run,
   such as a reader with no matching sample.

The finding is specific and gives a way forward:

```text
CU201 duplicate  kit.time.parse_ts matches core.time.parse_instant@2 on 50 of 50 probes.
      Use it:   axi data utils use core.time.parse_instant --into .
      Or state why not in utility.toml:  [[divergence]] from = "core.time.parse_instant"
CU202 near-dup   kit.time.parse_ts agrees with core.time.parse_instant@2 on 47 of 50 probes;
      differs on naive input (yours assumes UTC, core refuses unless assume="utc").
```

In the sandbox these are warnings. At promotion a duplicate is blocking unless the
manifest carries a `[[divergence]]` entry with a reason, and `kit-review` shows every
duplicate, near-duplicate and divergence to the approver beside the bundle.

**Consolidation** has three verbs on the catalog, each recorded in its events:

- `utils promote <name> --to <scope>`: a site or domain utility moves up (to the
  domain pack or to `core`) by the receiving scope's release process. The old name
  becomes an alias of the new one, so every pipeline that named it still resolves,
  to the same behaviour.
- `utils deprecate <name> --use <replacement>`: still resolves, and `kit-check` warns
  on any new use (CU204).
- `utils alias <name> <target>`: two names that the behaviour pass proves equal
  collapse to one; the alias resolves with the target's version.

A periodic sweep (`utils duplicates`) runs pass 2 and 3 across a whole scope and
writes a report of clusters, so consolidation of existing code is a list, not a
search.

### D4. A site's conform is a declared pipeline of utilities

A kit or site declares its conform as `pipeline.toml`, the way a Lakeflow declarative
pipeline names its datasets:

```toml
[pipeline]
name   = "example_loop.signals"
source = "rig-a"                                  # a source in kit.toml

[[step]]
use    = "core.readers.csv_long@1"
params = { time_column = "Time", nanoseconds_column = "nanosecs" }

[[step]]
use    = "core.normalize.wide_frame@1"
params = { channel_map = "site/channel-maps/rig-a.toml" }

[[expect]]
use       = "core.quality.expect_range@1"
params    = { channel = "temperature.*", min = -50, max = 400 }
on_violation = "mark_invalid"                    # warn | mark_invalid | fail

[[derive]]
use    = "example_loop.derive.delta_between@1"
params = { a = "temperature.outlet", b = "temperature.inlet", into = "temperature.rise" }
```

An expectation's three actions map Databricks' three onto silver's rules: `warn`
keeps the row and counts the violation; `mark_invalid` keeps the reading with its
value withheld and its quality and reason set, because silver never drops a reading;
`fail` refuses the pass for that source and leaves the watermark where it was
(ADR-193). Violation counts are recorded per expectation per pass, for the freshness
and health surfaces to read.

**A pipeline that names only catalog utilities and their parameters is a
declaration.** It promotes through ADR-203 as data, today, with no contributor code
shipped: the node runs `core` and domain utilities it already has installed, with the
parameters the pipeline gives. This is the main reason to compose rather than copy.

**Escape hatch.** A step may name a `kit` utility, which is the contributor's own code
with its own manifest and tests. It passes the same gate (D3, D7). It runs in the
sandbox at once and on the node when the isolated conform worker exists (ADR-203
decision 6, phase 3); until then the pipeline promotes and that step's code goes by
pull request, as today. Bespoke code is allowed; unnamed, unmanifested code is not.

### D5. Silver rows say which utility versions made them

- Each pipeline run records a **lineage entry**: the pipeline name, and for each step
  the utility name, version, and a hash of its parameters. The entry's hash is what
  `conform_version` holds on each silver row it writes (the column exists, ADR-206).
  The entries live in `utility_catalog.lineage`, expand-only, so the hot table gains
  no column.
- A derived row's `derivation` names its utility: `derived:example_loop.derive.delta_between@1`.
- A version bump is required when the golden outputs change (CU005). A change that
  keeps every golden output is a patch.
- **Rebuild by utility.** `conform-run --rebuild --utility core.time.parse_instant@<2`
  finds every lineage entry that used a matching version, turns them into the
  connector, site and time windows they covered, and runs the ADR-206 scoped rebuild
  for each, with `--dry-run` counts first. Because `conform_version` is not part of
  content (ADR-206 decision 2), rows whose content the new version reproduces are left
  untouched; the lineage table records that the window was re-verified under the new
  version. So `conform_version` on a row means "the latest version that changed this
  content", and the lineage table answers "which versions have re-checked it".

### D6. Who can promote what, and where controlled code goes

| Scope | Who promotes | Route |
|---|---|---|
| `kit` | The contributor | Nothing to promote; private to them. |
| site | The site's govern role (ADR-151), on `kit-approve` | ADR-203 bundle; code waits for the conform worker. |
| domain pack | The pack's maintainers | Pull request and release of the pack. |
| `core` | The platform owner | Pull request and release of the platform. |

- Who may discover, use or read a utility, and how that widens on promotion, is D9.
- **Export control applies to code.** A utility whose code is export-controlled carries
  `export_class = "controlled"`. Its source is readable only by principals holding the
  controlled-code clearance (D9 `read_code`), and only on a node authorized to hold
  it; a site may still let a partner `use` it, which runs it where it is held and
  returns only its output. It never appears in a public mirror or a community pack. A pipeline that names a
  controlled utility is itself controlled. A declaration-only utility over data the
  reader may already see carries the class of the code it names, not of the data.

### D7. One set of contributor conventions, enforced in the same places code is checked

The rules are one table in one module (proposed: `data_platform/utilities/conventions.py`),
and every place that checks a utility calls that table, so a rule cannot pass locally
and fail at promotion.

| Rule | What it requires |
|---|---|
| CU001 name | `<scope>.<area>.<verb_noun>`, lower snake case, area from the controlled list. |
| CU002 scope | The scope prefix matches where the utility lives and the promoter's authority. |
| CU003 manifest | `name`, `version`, `kind`, `summary`, `owner`, `entry` or `sql`, `tier_writes`, `export_class`, `[inputs]`, `[outputs]`, `[example]`, `[tests]`. |
| CU004 people | `owner` and each contributor are principal handles (`@name` or `@name:context`, single `@`), resolvable in the directory; an owner may be a group. |
| CU005 version | Semantic version; a change to any golden output needs a major or minor bump. |
| CU006 licence | A licence is present, inherited or allowed by the site (D10); explicit for publication. |
| CU101 typing | Every public entry is fully annotated (ruff `ANN`). |
| CU102 docstring | A summary and the manifest's example, runnable as a doctest (ruff `D`). |
| CU103 units | A numeric output carries a declared unit; no bare scale literal (`* 1000`, `/ 60`) outside `core.units` (the check `tiers.VIEW_TRANSFORM_SIGNATURES` already makes for gold SQL, extended to Python). |
| CU104 purity | `function`, `normalizer`, `expectation` and `derivation` import no I/O (files, network, database, environment); a `reader` reads only the handle it is given (ruff `TID251` banned imports plus a call check). |
| CU105 determinism | No wall clock, unseeded randomness or random identifiers; the golden tests are run twice and must match. |
| CU106 time | No naive datetimes (ruff `DTZ`); parse through `core.time` or say why in `[[divergence]]`. |
| CU107 tests | Golden tests exist, cover the example, and pass. |
| CU201 duplicate | D3, blocking at promotion unless a divergence is declared. |
| CU202 near-duplicate | D3, a warning shown to the approver. |
| CU203 copy | D3 structural match with a catalog utility; blocking at promotion. |
| CU204 deprecated | Using a deprecated utility warns; a retired one is an error. |
| CU301 references | Every pipeline step resolves and is version-pinned. |
| CU302 tier | A utility writes only the tier it declares, and a kind writes only what the D1 table allows (ADR-128). |
| CU303 class | A pipeline's export class is at least that of every utility it names. |
| CU304 consumers read gold | Serving and consuming code (apps, charts, chat and MCP tools, gold verbs, a kit's consuming code) reads gold only; a silver or bronze read is allowed only in producing and debugging paths, and must be explicit and labelled (D11, ADR-128 E4). |

Where each runs:

| Place | Rules | How |
|---|---|---|
| Editor and ruff | CU101, CU102, CU104 (imports), CU106 | A ruff profile the kit template ships and site and consumer repositories extend. |
| Guard test in each repository | CU001 to CU006, CU101 to CU107, CU301 to CU304 | `tests/test_conforming_utilities_guard.py`, which calls `utils lint --all`; the same test is ported to the consumer and site repositories, as other guards are. |
| Pre-push hook | the ruff profile and the guard test | The existing hooks; no new hook. |
| GitHub Actions | the same | The existing `ci.yml` job runs both. |
| `kit-check` | all, with D3 as warnings | Locally, before promotion. |
| Node submit | CU001 to CU006, CU301 to CU304, D3 | The static half, re-proved on the node (ADR-203 decision 3). |
| `kit-review` and `kit-approve` | shows every finding; refuses blocking ones | `--override` with a reason, recorded in `kit_registry.events`. |

### D8. Onboarding: Part 2 teaches finding and using utilities, and ends with your own

The colleague guide's Part 2 (adding a data kit) gains a step before "shape it": find
what exists. Its culminating step is that a contributor's own promoted utility appears
in the same listing they started from. This is the design's acceptance test, rehearsed
by a test rather than by hand:

| Step | Command | What the person sees |
|---|---|---|
| 1. Find | `axi data utils list --usable` | `core`, domain and their site's utilities: name, kind, version, owner, summary. |
| 2. Search | `axi data utils search "range check"` | `core.quality.expect_range`, with its summary and example. |
| 3. Read | `axi data utils show core.quality.expect_range` | The manifest, the example call and result, versions, and how many pipelines use it. |
| 4. Use | `axi data utils use core.quality.expect_range --into . --param channel=temp.* --param max=400`, then `kit-try` | A new `[[expect]]` in `pipeline.toml`; `kit-try` shows the rows it marks. |
| 5. Duplicate on purpose | `axi data kit-scaffold utility time parse_ts`, then `kit-check` | CU201: matches `core.time.parse_instant`; use it or state why not. |
| 6. Contribute | `axi data kit-scaffold utility derive delta_between`, fill it in, `kit-check` | `ready`, with the utility's manifest checked and no duplicate. |
| 7. Promote | `axi data kit-promote`; an operator runs `kit-review`, `kit-approve` | The bundle `applied`. |
| 8. See yours | `axi data utils list --mine`, then `utils show <site>.derive.delta_between` | Their utility, version, owner = them, state `applied`, and a usage example. |
| 9. Use yours | In a second kit: `utils use <site>.derive.delta_between --into .`, `kit-try` | It runs. |

**Proof.** `test_part_two_utilities_rehearsal` runs these steps against a local node
and a kit sandbox, with a fixture operator for step 7, and asserts: step 1 lists the
`core` utility; step 5 reports CU201 naming it; step 8 lists the new name with the
rehearsal principal as owner and state `applied`; step 9's `kit-try` succeeds. It runs
in the consumer's onboarding smoke (the published-package path) and in the merge
queue. The guide's commands are checked by the markdown CLI linter
(`scripts/lint_markdown_cli_commands.py`), so the guide cannot name a verb that does
not exist.

Until a Python utility can run on the node, step 6 contributes a declaration-only
utility (a derivation expression, an expectation over catalog parameters, or a gold
view and verb), which promotes today. Step 6 with Python follows the conform worker.

### D9. Visibility and sharing are authorization tuples

How far a utility is visible is configured, widens only by an approved step, and is
held in the authorization model GUARD already uses (ADR-083, the OpenFGA substrate in
`authz/openfga.py`, the per-site model `authz/fga/site.fga`). There is no second
permission system for the catalog.

**Three permissions, kept separate.**

| Permission | Grants | Why it is separate |
|---|---|---|
| `discover` | That the utility exists: its name, manifest, summary, example, versions | Finding a utility is how duplication is avoided; it should reach as far as policy allows. |
| `use` | Naming it in a pipeline or kit; the node runs it and returns its output | Running a utility does not require seeing its code. |
| `read_code` | Its source and its golden tests | Export control is about code, so a site may let a partner use a controlled utility without reading it. |

`read_code` implies `use`, and `use` implies `discover`. `read_code` also needs the
utility's clearance (below), so no grant of visibility can expose controlled source to
someone not cleared for it.

**Six levels.** Each is a set of tuples, written when the level is approved and deleted
when it is withdrawn. "Promote" is an approved tuple write; "withdraw" is a tuple
delete. Each utility version is its own object (`utility_version:<name>@<version>`),
so a new version starts private and old versions keep the visibility they had.

| Level | Who sees it | Tuples written on `utility_version:V` | Approver |
|---|---|---|---|
| private (default) | The contributor and agents they delegate to | `owner@user:<c>`; agents follow from `user:<c>#delegate@agent:<a>`, already held for the person | None; it is the starting state |
| kit (team) | The kit's contributors | `discover`, `use`, `read_code` `@kit:<k>#contributor` | The contributor, if `promote_default` allows it; else the kit's owner |
| site | People granted at the site | `discover`, `use` `@site:<s>#viewer`; `read_code @site:<s>#viewer` unless narrowed | The site's govern role, on `kit-approve` (ADR-203, ADR-151) |
| program | People of every consenting member site, and program grantees | `discover @program:<p>#participant`; `use` and `read_code` as the program's policy says | Automatic on site promotion if the policy is `on_site_promote`; else the program steward on request |
| platform (core, domain) | Everyone on the deployment | `discover`, `use`, `read_code` `@user:*` | The platform or pack maintainer, through the release |
| community (future) | Other deployments | Published through federation to a community pack; a receiving deployment writes its own `@user:*` on install | A publication review by the platform owner, with an explicit licence in the manifest |

`user:*` means every authenticated principal of one deployment, because each
deployment has its own authorization store. Public sharing is therefore a federation
act (publish, review, install), never a wildcard written into someone else's store.

**The model diff** (added to `authz/fga/site.fga`; `user` gains one relation, and nothing else that exists changes):

```text
type agent
  relations
    define delegator: [user]

type user
  relations
    define delegate: [agent]

type clearance
  relations
    # clearance:open#holder@user:*  (everyone)
    # clearance:controlled#holder@group:controlled-code#member
    define holder: [user, user:*, group#member]

type program
  relations
    define steward: [user, group#member]
    define member_site: [site]                 # written when a member site consents (ADR-205 D2)
    define grantee: [user, group#member]       # ADR-205 program grants
    define participant: grantee or viewer from member_site

type kit
  relations
    define site: [site]
    define owner: [user]
    define contributor: [user, group#member] or owner

type utility_version
  relations
    define owner: [user]
    define clearance: [clearance]              # exactly one: open or controlled
    define discover_grant: [user, user:*, group#member, kit#contributor, site#viewer, program#participant]
    define use_grant:      [user, user:*, group#member, kit#contributor, site#viewer, program#participant]
    define code_grant:     [user, user:*, group#member, kit#contributor, site#viewer, program#participant]
    define blocked: [user, group#member]
    define read_code: (owner or delegate from owner or code_grant) and holder from clearance
    define use: owner or delegate from owner or use_grant or read_code
    define discover: owner or delegate from owner or discover_grant or use
```

`blocked` keeps GUARD's deny-overrides rule: the substrate checks it first and a hit is
a refusal whatever else is granted. A version is written with exactly one `clearance`
tuple, from its manifest's `export_class`.

**The catalog's reads are authorization calls.**

- `utils list --usable` is `ListObjects(user, "discover", "utility_version")`, then a
  catalog read of those objects. A thin `list_objects` method is added beside
  `check`, `list_members` and `write` in `authz/openfga_http.py`.
- `utils show` checks `discover`; it shows the source only after a `read_code` check.
- A pipeline step resolves only if `use` holds for the submitting principal, checked by
  `kit-check` locally and again by the node's submit.
- A program member's view is clipped by ADR-205 as for data: `member_site` is written
  only after that site's administrator consents, and deleted when it withdraws.

**Configuration.**

- Site configuration `promote_default = "private" | "kit" | "site"` sets what
  `kit-promote` asks for when the contributor says nothing. **The default is `site`**
  (owner decision, 2026-10-28): a promoted kit utility is visible at its site unless
  the site or the contributor narrows it. Before promotion a utility is private.
- A program's terms gain `share_utilities = "on_site_promote" | "on_request" | "never"`,
  plus whether `read_code` is included. **A program member may `use` a utility without
  `read_code`, across programs** (owner decision, 2026-10-28). The utility runs on the
  node that owns it and returns only its output, and `read_code` still needs the
  utility's clearance. It is a term like ADR-205's `tiers`, so each
  member site's administrator consents to it once, and widening it needs consent again.
- A contributor may narrow in the manifest (`[visibility] max = "site"`,
  `read_code = false`) but never widen beyond the site's `promote_default` and the
  program's terms. Every level above site names its approver (the table above).

**Every tuple write is audited.** Writes go through one skill (`utils share`,
`utils unshare`, and the promotion paths that call it), never straight to the store.
Each write records the tuples, the approver, the reason and the bundle in
`utility_catalog.events` and in GUARD's audit trail (`axiom.infra.audit_trail`), and
a program-level write shows in the audit view of each affected site (ADR-205 D9).

**The duplicate check sees only what its contributor may discover.** D3's candidate
set is the contributor's `ListObjects` for `discover`. The behaviour pass runs only
candidates the contributor may `use`, and the structural pass compares only code they
may `read_code`. A finding names a utility the contributor can already see and never
quotes code they cannot read, so private and controlled code never leaks through a
duplicate report. Duplicates across scopes a contributor cannot see are found by the
maintainers' `utils duplicates` sweep, which runs with their own authority.

### D10. A utility's licence defaults from the site's owning organization

Owner decision, 2026-10-28.

- Every utility carries a licence. Unless its manifest says otherwise, it inherits the
  licence default of the organization that owns the site it is promoted at, set in
  site configuration as `utility_license_default` (an SPDX identifier).
- The site configuration also lists the licences a contributor may choose instead
  (`utility_licenses_allowed`). A contributor may set a licence in the manifest only
  where the site allows it and only one compatible with the site's default; `kit-check`
  and the node submit refuse anything else (rule CU006 licence, added to D7, running
  wherever CU003 runs).
- Community or public publication requires an explicit licence in the manifest at
  publish time; an inherited default is not enough, and the publication review checks
  it.

### D11. Corrections live in conform; a SQL repair is a fast path that must be proven

Owner decision, 2026-10-28.

- **Bronze is never modified.** Every correction to data lives in bronze-to-silver
  conform logic: a declared catalog utility (reader, normalizer, expectation,
  derivation) or its declaration in a channel map or pipeline. Silver is therefore
  always derivable from bronze plus declared, versioned logic, which is what D5's
  rebuild by utility version relies on.
- **A direct SQL change to silver** (a relabel, a delete, a collapse) is allowed only as
  an operator fast path, and only when both hold:
  1. every removed or changed row is first copied to the identity audit table
     (`silver.signals_identity_audit`, ADR-206) with a reason; and
  2. it is followed by `conform-run --rebuild --dry-run` over the same scope, which
     reports **0 inserted and 0 replaced**, proving that the declared logic reproduces
     the patched silver.

  If the dry run reports changes, the patch was wrong and the rebuild wins. The
  dry-run result is recorded with the repair, in the audit row and the catalog events.
  A SQL repair without a matching declared change cannot pass condition 2, so the
  correction always ends up in conform.
- **Data fixes become catalog utilities where they generalize.** The first entries are
  the fixes already made in conform code: sub-second timestamp parsing (merging a
  separate sub-second column into the instant), per-run clock correction, the feed or
  stream key fallback, and the sentinel expectation. The first two are the
  consolidation targets that seed `core.time` (inventory section 1).
- **Consumers read gold.** Serving surfaces (apps, charts, chat and MCP tools, gold
  verbs, and a kit's consuming code) read gold only. Reading silver or bronze is for
  producing and debugging: `kit-try` and `kit-check`, conform diagnostics, and the
  data-kit authoring loop (ADR-203 decision 9). Such a read is explicit and labelled,
  never a shortcut in a consuming layer. This restates ADR-128's tier boundaries
  (E4: the tools that maintain a tier read and write it) for utilities, and the
  convention lints enforce it as rule CU304 (D7).

## Consequences

- A contributor can see what exists before writing anything, and is told at
  `kit-check` when they are about to add a copy, with the name of what to use.
- A site's conform becomes mostly parameters to shared utilities, which promote as
  data under ADR-203 without waiting for the conform worker.
- Fixing a shared utility fixes every pipeline that uses it, and `rebuild --utility`
  says exactly which silver windows to re-derive. The other side: a bad release of a
  `core` utility affects every site at once, so `core` utilities need the golden and
  probe tests this ADR requires, and a site can pin an older version while it checks.
- The catalog adds state: `utility_catalog.utilities`, `.lineage`, `.events`, all
  expand-only (ADR-182).
- Contributors carry a manifest per utility. `kit-check` writes the first draft, so
  the cost is the fields only a person can fill in.
- The platform's own duplicates (inventory section 2.3) become the first entries of
  `core`, which is work the platform owes regardless.

## Phases

1. **Discovery over what exists (no new storage).** `data utils list / show / search`
   project today's registries into one listing with synthesized manifests: the gold
   skills, the kit registry's verbs, charts and automations with their owners, the
   normalizers discovered by entry point, the chart registry, and the declared
   expectation. Part 2 gains steps 1 to 3 and 8 (with a verb as the contributed
   utility). The rehearsal test, in its first form.
2. **Manifests, conventions, visibility and the first `core` areas.** The D9 model
   types and the share skills, with `list --usable` moving onto `ListObjects`. `utility.toml`, the CU rules
   in every place D7 names, `core.time` and `core.units` (the two largest duplicate
   sets), the catalog table, and the signature and behaviour passes of D3 for those
   two areas. `utils use`.
3. **Declared pipelines and lineage.** `pipeline.toml`, the lineage table,
   `rebuild --utility`, and the first site conform moved to a pipeline.
4. **Consolidation at scale.** The structural pass, `utils duplicates`, promote,
   deprecate and alias, program-wide listing, the web catalog page.

## Open questions

- Should `core` utilities live in a new package (`axiom.utilities`) or stay in their
  current modules with manifests pointing at them? A new package makes the boundary
  visible and the guard simple; it costs import churn across about 40 call sites.
- The behaviour threshold for "near-duplicate" (proposed: at least 80% of probes
  equal, at least one different) needs measuring on the real copies first.
- Should a site be able to auto-approve a pipeline that names only `core` and domain
  utilities (ADR-203 phase 4)? It carries no new code.
- How a site that withholds its utilities from a program (ADR-205) still contributes
  to `utils duplicates` sweeps run by the domain maintainers: by fingerprint only, or
  not at all.
- Whether consolidating the persisted hashes (inventory section 1) is worth it at all,
  given that changing a hash changes identity.
- Whether a community utility may depend on a non-community one, and who performs
  the publication review.
- Whether ADR-205's grants and program membership are stored in the authorization
  model directly, or projected into it from ADR-205's own tables. D9 needs the tuples
  either way; projecting means two stores to keep in step.
