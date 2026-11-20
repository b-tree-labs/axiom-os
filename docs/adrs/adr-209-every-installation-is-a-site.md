# ADR-209: Every installation is a site

**Status:** Proposed (2026-10-28, draft for the owner's review)
**Amends:** ADR-050 (site and tenant vocabulary), ADR-178 (the site record), ADR-195 and
ADR-207 (binding and serving mode), ADR-019 and ADR-164 (node profile and functions)
**Supersedes:** the `platform` FGA type of ADR-050 and ADR-083, which becomes `org` (D7). Those
accepted ADRs are not edited in place.
**Related:** ADR-130 (federated site identity, multi-site membership), ADR-151 and ADR-153
(roles granted at a site), ADR-205 (person keys and sharing programs, PR #1440), ADR-208
(conforming utilities, PR #1458, which depends on this ADR for `promote_default` and the
licence default), ADR-120 (tenancy control plane), ADR-131 (there is always a medallion),
ADR-170 (release slots), ADR-203 (a data kit is promoted as data)
**Evidence:** [site primitive inventory](../working/site-primitive-inventory-2026-10-28.md),
[owner memo](../working/site-primitive-memo-2026-10-28.md)

## Context

"Site" is already the axis that most of Axiom keys on. A credential belongs to one site
(`@name:site`), gold refuses a table with no site column, the gate authorizes a read
against a site, ingest takes its site from the credential, a role is granted at a site
(ADR-153), and a laptop's reads follow its binding to a site's node (ADR-195, ADR-207).
What Axiom does not have is the site itself. The inventory found:

- **No site record.** ADR-178 names the site registry as missing, and it still is. A site
  is implied by the connectors, keys, roles and environment variables that mention it.
- **No writer.** Axiom reads `site-bindings.json` but nothing in Axiom writes it. The
  binding is written, and the site repository is scaffolded and deployed, by a domain
  consumer's `site` command. Axiom's own refusal text tells a person to run that
  consumer's command.
- **Many readers, no resolver.** About a dozen modules read `AXIOM_SITE` directly, with
  three different fallbacks (`None`, `"local"`, the credential's claim). Beside it sit
  `AXIOM_SERVED_SITES`, `AXIOM_HOME_SITES` and the binding's `site` and `key_site`. The
  consumer adds four more resolvers of its own.
- **Four id formats** (webauth, kit, principal context, producer id), **three names for
  one concept** (`site`, `site_id`, `tenant`), and an alias table
  (`infra/site_identity.py`) that is applied by some readers and never loaded.
- **Nine site-shaped config files** in three base directories, and no schema for the one
  file every site repository actually carries (`site.toml`).
- **An authorization model that is not loaded.** `authz/fga/site.fga` defines `site` and
  `platform`; no code loads it, and ADR-205 and ADR-208 each propose a `program` type
  that is not yet in it.
- **Site repositories carry platform code.** The host site repository carries about
  9,000 lines of serving code and 8,700 lines of scripts. The partner repositories are
  three copies of the same scaffold (brand module, deploy script, unit files, CI) with a
  name swapped.

The owner's direction (2026-10-08, restated 2026-10-28) is that site is the deployment,
branding and tenancy unit of any Axiom install, that nothing runs un-sited, that
"generic" means the default site, and that the concept belongs entirely to Axiom. A domain
consumer should do almost nothing about sites except add its own fields.

## Decision

### D1. A site is one record, and every installation has at least one

A **site** is the unit that owns data, carries a brand, sets policy and is the boundary
of tenancy and federation. It is described by one record:

| Field | Meaning | Today |
|---|---|---|
| `uuid` | Immutable identity, minted at creation, never reused or changed. The record keys on it. | none |
| `slug` | Readable id, `^[a-z][a-z0-9-]{1,47}$` (the kit format). What rows, tuples, keys and principal handles carry. Renaming it is governed (D8). | four formats |
| `aliases` | Former slugs, resolved to `slug` everywhere (the `site_identity` table, now loaded from the record). A rename adds the old slug here; no row is rewritten. | env table, never loaded |
| `name` | Display name. | none |
| `org` | The owning organization (`@org`). Exactly one. Accountability does not split. | none |
| `alternates` | Backup organizations, in order, that may act for the site when its owner cannot (D1a). | none |
| `visibility` | `personal`, `org`, `shared` or `public` (D2). | none |
| `brand` | Name, accent, logo, support address, tagline. | `AXIOM_BRAND_*` env, per-repo `gate_brand.py` |
| `profile` | The node profiles and functions the site's nodes declare (D3). Node profile is a part of site profile. | `node.toml`, unrecorded profile |
| `policy` | Site rules, autonomy floor (ADR-153 §6), classification ceiling, agent policy, maintenance level. | `site_rules.toml`, `node.toml`, `serving_policy.toml` |
| `licence` | `utility_license_default`, `utility_licenses_allowed`, `promote_default` (ADR-208 D9, D10). | none |
| `tenancy` | The data tenant is the site id. Schemas, roles and row-level-security keys derive from it. | `kit.toml tenant`, `tenants/<site>.toml` |
| `federation` | Edges it accepts, `accept_identity_from`, programs it has consented to (ADR-205 D2). | topology file, federation store |
| `nodes` | The nodes that serve it and the function each performs. | `AXIOM_SERVED_SITES` |
| `ext.<pack>` | Fields a domain pack adds (D6). Axiom stores and validates them; it does not interpret them. | domain keys inside `[site]`, free text |

The record has two halves with different owners. **Declared** fields (everything above)
come from the site's configuration store (D5) and change through it, by the tiered
process in D8. **Runtime** fields
(status `pending | active | suspended | removed`, issued key ids, consents, last
heartbeat) are held by the host node's registry, as ADR-178 D1 already says. The
registry is the materialized form of the declared half plus the runtime half; it is never
the place where a declared field is first edited.

**Slugs and addressing** (proposed).

- An org site's slug is `<org>-<place>`, where the place names the site within its org
  (a consumer may call that part by its own term). Its repository is `<slug>-site`, which
  matches the consumer's repository-naming rule.
- A personal site's slug is `<handle>-<device>`: the handle from the installing
  principal, the device from the host name, both lowercased with every other character
  turned into a hyphen. It is never built from an email address.
- A site is addressed with the principal syntax: `@<slug>`, or `@<slug>:<org>` where the
  org is not implied.

**Every installation gets a default site at setup.** `axi setup` creates a `personal` site
owned by the installing person, with the `<handle>-<device>` slug. Setup shows the slug
and lets the person change it once before anything is written under it; a later change
is a governed rename (D8). The site is stored in the local config store. A bare install, a personal harness and a hosted tenant are all
sites. "Generic" means the default site, never "no site". The legacy setup step that
writes a free-form site file is replaced by this.

### D1a. Alternates: who acts when the owner cannot (decided: one owner, with alternates)

A site has exactly one owning org and may name **alternates**: backup orgs, in order,
whose administrators can act for the site when the owner's cannot (succession, an absent
administrator, an emergency). Naming or removing an alternate is a governed change (D8).
The alternate has to accept, and its acceptance is recorded.

An alternate exists to keep a site running and safe and to hand it on. It never widens
what the site shares.

| An alternate's administrator may | Conditions |
|---|---|
| Approve a governed change that keeps the site running (renew a key, restore a node, change a maintainer) | The owner is notified at once and can reverse the change |
| Suspend the site, withdraw it from a program, narrow its visibility, rotate or revoke keys | Takes effect immediately. The owner is notified, and these are all reversible. |
| Appoint or replace the site's administrators (succession) | Takes effect after the site's `succession_wait_hours` window unless the owner objects |
| Transfer ownership to itself or to another org | Same `succession_wait_hours` window with no objection. It is recorded as a transfer, never as an edit. |

An alternate's administrator may **not**:

- widen visibility, join a program, accept new terms, change the licence or open a
  federation edge;
- read the site's data. Being an alternate grants no data access. Reading data needs its
  own grant (ADR-205 D3).

**The succession wait (decided, 2026-10-28, default and floor both accepted).** `succession_wait_hours` is a per-site
config field. It defaults to 72 and is governed (D8). It has a floor of 24 hours, so it
can never be set to 0: a value below the floor is refused by `axi site validate`, and an
emergency override does not bypass it. The point of the floor is that an owner always has
time to object before its site is taken over. Lengthening the wait needs only the owner's
approval. Shortening it is also governed, and the alternates are notified.

Every alternate action is a governed action and is audited. It names the alternate, the
reason given and the owner's response.

### D2. Visibility is a property of the site, written as authorization tuples

| Visibility | Who may discover it | Who may read its data |
|---|---|---|
| `personal` | Its owner | Its owner and their delegates |
| `org` | Members of the owning org | People granted at the site (ADR-153, ADR-205 D3) |
| `shared` | Members of the owning org, and participants of programs it has consented to | Site grantees, plus program grantees, clipped to the consented terms |
| `public` | Anyone, and its published snapshots are listed | Published snapshots only, served from object storage (D9). On the node, as `shared`. |

Visibility only sets defaults. It never widens a read past ADR-205's terms or a
classification ceiling, and a site in no active program stays a silo (ADR-205 D4): to
everyone without a grant it does not exist. Visibility is applied by writing tuples
(D7), through one audited skill, so "who can see this site" has one answer.

Changing visibility toward `public` is a governed change (D8). The `public` tuple only
ever makes a site discoverable; it never grants an anonymous read on a node (D9).

### D3. Cardinality

**Node and site, many to many.**

- A node serves one or more sites. It always serves at least one, its own default site.
  A host node serves its own site and its tenants (ADR-178 kinds `host` and `tenant`).
- A site may span several nodes, each declaring its functions (ADR-164). Exactly one node
  holds a site's medallion at a time (ADR-131); the record names it.
- A node belongs to exactly one host, the operator that runs it (D7). On a self-run
  install, the host and the owning org are the same party.
- A laptop has its own personal site **and** any number of **bindings** to other sites'
  nodes (ADR-195, ADR-207). A binding is not membership; it is the route by which this
  install reaches a site it holds a grant at.

**Which site is this command acting in.** One function, `axiom.site.active()`, answers it,
in this order: an explicit `--site` flag; `AXIOM_SITE`; the `site.toml` of a site
repository found by walking up from the working directory; the binding marked current;
the default site. It returns the site record, the source of the answer and the reason.
Serving mode (ADR-207) then resolves **for that site**: `hosted` if this install holds
that site's medallion, `bound` if it holds a binding to it, `local` if it is this
install's own site, `test` if sample data is set. `resolve_mode()` keeps its contract and
gains the site as an input. A command acts in exactly one site; a cross-site view is an
explicit fan-out, one query per site (ADR-205 D6), never a union.

**Person and site, many to many, through grants.** A person has a home site and holds
ADR-205 grants `(principal, role, where, expires_at)` with `where` one of `site:<id>`,
`program:<id>` or `*`. ADR-205's draft spelling `sharing:<id>` is accepted as an alias of
`program:<id>`. One credential, session or assertion names one active site (ADR-130 §5).

**Site and program, many to many, by consent.** A program is not a site. It is an
agreement among member sites (ADR-205 D2) with stewards and versioned terms. A site joins
only by its administrator's consent and may withdraw at any time.

**Org and site, one to many.** An org owns many sites; a site has one owning org.

### D4. One site id, one resolver, one place config lives

- **One id format** (D1). Existing ids that do not fit become aliases of a conforming id;
  nothing is renamed in place (the existing `data.site_rename` skill handles rows).
- **One reader for "this node's sites".** `axiom.site` exposes `active()`, `served()`
  and `home()`. Every module that reads `AXIOM_SITE`, `AXIOM_SERVED_SITES` or
  `AXIOM_HOME_SITES` today moves to these. The variables remain as overrides (D10).
- **Alias resolution is applied by the resolver**, so every reader gets it. Today only
  three do.
- **One name in code:** `site`. `tenant` remains the ADR-050 word for the isolation
  concept in prose and for the identity-provider tenant; `kit.toml`'s `tenant` key is read
  as `site` with a notice.
- **One config base.** Site files live under `get_platform_state_dir()/sites/<id>/`; the
  hard-coded `~/.axi` paths go.

### D5. The site repository is an Axiom pattern

A site whose configuration is shared or reviewed keeps it in a **site repository**; a
personal site may keep it only in the local store. Axiom owns the template, the verbs and
the guards. A domain pack contributes to the template (D6). Repository names follow the
consumer's rule; Axiom requires only the `-site` suffix.

**Verbs.** The noun is `axi site` (decided), and ADR-178's `axi sites` stays as an alias:

| Verb | Does |
|---|---|
| `init <id> --org <org> [--pack <name>]` | Scaffolds a site repository from `axiom-site`, plus the pack's contributions. |
| `validate` | Validates `site.toml` against Axiom's schema and each pack's schema. |
| `lint` | Runs the site-repository rules below. |
| `deploy` | Installs a release into an ADR-170 slot, flips `current`, prunes; generates unit files and brand assets from the record. |
| `bind`, `redeem`, `use`, `unbind`, `whoami` | Write and select bindings (`site-bindings.json`, format unchanged). |
| `show`, `list`, `checklist` | Read the record and the ADR-178 checklist. |
| `add`, `suspend`, `resume`, `remove`, `rotate-key` | ADR-178 D2 admin verbs for hosted sites. |
| `visibility <level>`, `consent <program> --terms <v>`, `withdraw <program>` | D2 and ADR-205 D2, as audited tuple writes. |
| `config set <field> <value>`, `config tier <field>` | Opens a change on the site repository, by the D8 process; never edits live. |
| `alternate add / remove / act` | D1a. |
| `publish <dataset> --version <v>` | D9. |

All are skill functions (ADR-056): CLI, MCP and agent surfaces call the same function.
Mutating verbs need the site's administrator role and are never auto-approved for an agent.

**A site repository may contain:**

- `site.toml` (the declared record) and a single pins file;
- content: brand assets, prompts, documents, guides, sample data;
- declarative data: channel maps, instrument declarations, packs, verbs, gold view SQL,
  `data/kit.toml` and `pipeline.toml` (ADR-208 D4);
- site utilities, as ADR-208 catalog objects with manifests, under `data/conform/`;
- tests that assert the site's own configuration and data;
- deploy values (compose or chart values) that parameterize Axiom's artifacts.

**It must not contain:** generic code (modules a second site could use unchanged),
serving applications, vendored copies of platform modules or scripts, or files the
template generates.

**Guards** (`axi site lint`, run by a reusable `site-ci` workflow that Axiom publishes):

| Rule | Refuses |
|---|---|
| SR001 code | Python or JS outside `data/conform/`, `tests/` and the pack's declared extension points. |
| SR002 managed files | A committed file that matches a template-generated file (brand module, deploy script, unit file). It is generated at deploy. |
| SR003 pins | Platform versions pinned in more than one file, or pins that disagree. |
| SR004 schema | A `site.toml` that fails the Axiom schema or a pack's schema. |
| SR005 identity | A record whose `id`, `org` or `visibility` is missing, or an id that is not canonical and not an alias. |
| SR006 secrets | A key or token value in the repository. Only vault references are allowed. |
| SR007 utilities | A conform module without an ADR-208 manifest. |

Existing debt is listed by file and may only shrink, as the domain-name guard does.

### D6. A domain pack extends a site; it does not own the concept

A pack registers a **site extension** through the entry-point group
`axiom.site_extensions`:

```python
SiteExtension(
    name="example-domain",
    schema=...,             # JSON schema for [ext.example-domain]
    defaults=...,           # values init writes
    validators=[...],       # checks against the pack's own contracts
    data_kinds=["channel-maps", "instruments", "packs"],   # tiered site data it reads
    scaffold=...,           # files init adds to a new site repository
    profile=...,            # ADR-178 D6 onboarding profile (schemas, channel-map scaffold, guide)
)
```

```toml
[site]
id = "example-site"
org = "example-org"
visibility = "shared"

[site.brand]
name = "Example Site"
accent = "#336699"

[site.licence]
utility_license_default = "Apache-2.0"
promote_default = "site"

[ext.example-domain]
kind = "example-loop"        # a domain type, validated by the pack's own registry
```

Axiom validates `[ext.*]` with the registered schema, refuses an `[ext.x]` table whose
pack is not installed (unless `validate --allow-unknown`), and exposes it as
`site.ext("example-domain")`. Axiom also provides the one tiered search for site data
(built-in, user, bound site repository, project), so a pack names a data kind instead of
copying the search. Domain types (the kind of plant, an instrument vocabulary) stay in the
pack.

### D7. One authorization model (decided: `platform` becomes `org`; `host` and `node` added)

`authz/fga/site.fga` becomes the one model, loaded by the substrate (today only the
starter model is embedded). It has exactly these types, and every other place in this ADR
uses the same list:

**user, group, org, host, node, site, program, kit, utility_version, agent, clearance.**

- **`org`** replaces `platform`. It is the owning organization. `org#admin` is a viewer
  of every site the org owns, through `site#owner_org`. This is the same path that
  `platform#admin` plus `site#parent` gives today, with the same behaviour, under the
  right name. An org's members do not read its sites' data. They discover a site only
  when its visibility is `org` or wider (D2). The alternates in D1a are
  `site#alternate_org`, and their powers are the limited ones D1a lists.
- **`host`** is an infrastructure operator that runs sites for several orgs (a hosted
  tenant). Its relations are for operating: deploying, restarting and reading vitals.
  These are separate from reading data. **A host never implies viewer of a site.** It
  reads a site's data only when that site writes a grant to it
  (`site:<s>#viewer@host:<h>#operator`).
- **`node`** is a machine. It belongs to one host and serves one or more sites (D3). Its
  relations cover reading vitals and storage, which closes gap D of the bound-node vitals
  analysis (PR #1457). They also cover binding to the node (ADR-207).

```text
type user

type group
  relations
    define member: [user, group#member]

type org                                # was `platform`
  relations
    define admin: [user, group#member]
    define member: [user, group#member] or admin

type host                               # an operator running sites for several orgs
  relations
    define operator: [user, group#member]
    define deployer: operator
    define restarter: operator
    define vitals_reader: operator

type node
  relations
    define host: [host]
    define site: [site]                 # the sites this node serves (D3)
    define operator: [user, group#member] or operator from host
    define deployer: operator or deployer from host
    define restarter: operator or restarter from host
    # gap D: vitals and storage are an operational read, not a data read
    define vitals_reader: [user, group#member] or operator or vitals_reader from host or owner from site
    # ADR-207: a person may bind to a node that serves a site they can read;
    # every read through the binding is still checked against the site it names
    define binder: [user, group#member] or reader from site

type program
  relations
    define steward: [user, group#member]
    define member_site: [site]          # written on a member's consent, deleted on withdrawal
    define grantee: [user, group#member]
    # ADR-208 utility discovery: grantees and the viewers of member sites
    define participant: grantee or viewer from member_site

type site
  relations
    define owner_org: [org]             # replaces `parent: [platform]`
    define alternate_org: [org]         # D1a; written on the alternate's acceptance
    define program: [program]           # the programs this site has consented to
    define owner: [user, group#member]
    define editor: [user, group#member] or owner
    # org admin reaches every site the org owns; a host reaches one only by a grant here
    define viewer: [user, group#member, host#operator] or editor or admin from owner_org
    # D1a powers are checked by the skill, which also enforces the waits; FGA only says who.
    # An alternate gets no data read from this relation.
    define alternate_admin: admin from alternate_org
    # data read through a program: explicit program grantees only
    define program_reader: grantee from program
    define reader: viewer or program_reader
    # discovery, set by visibility (D2); public is discovery only, never a data read (D9)
    define org_visible: [org]           # written when visibility >= org
    define public_visible: [user:*]     # written when visibility = public
    define discover: reader or member from org_visible or participant from program or public_visible
    define blocked: [user, group#member]

# ADR-208 types, unchanged: agent, clearance, kit, utility_version
```

Three choices are deliberate:

- **A program reader is a program grantee, not a member site's viewer.** If `viewer` of
  one member site implied `reader` of another, membership would union silently, which is
  the failure ADR-130 §5 forbids. ADR-208's `participant` (which does include member
  viewers) governs only discovery and utility use.
- **Operating is not reading.** A host or node operator can deploy, restart and read
  vitals without seeing data, and a data viewer does not see vitals unless they own the
  site. On the HTTP side the same split is the `node` mount marked operational, which
  needs an explicit `node:read` scope (PR #1457, gap D, step 1).
- **Terms are not tuples.** Which tiers, classifications and domains a program read may
  return are ADR-205 terms applied at request time. The model answers "may this person
  read this site at all"; the terms clip what comes back.

The model above is a design sketch. Before M2 lands, it is validated with
`fga model test` against `site.fga.yaml`, rewritten from `platform` to `org` and extended
with the D2 matrix and the host and node cases.

### D8. Site configuration changes through the site repository, tiered by risk (proposed)

Git in the site repository is the source of truth for declared fields. Nothing edits a
declared field live. `axi site config set`, the Settings page and an agent all open a
pull request on the site repository. RIVET runs the process, because strict human gating
on every change has jammed CI/CD before. The tier decides how much human attention a
change needs, and a change takes the highest tier of any field it touches.

| Tier | Who merges | Before merge | After merge |
|---|---|---|---|
| **Routine** | RIVET, with no human involved | `axi site validate`, `axi site lint`, and `axi site deploy --dry-run` against the serving node | Deploy, then the post-deploy check |
| **Notable** | RIVET | The same checks | Deploy and the post-deploy check, then RIVET notifies the site maintainer, who can revert it with one action for `notable_revert_days` (merge first, review after; D8a) |
| **Governed** | RIVET, only after a recorded approval by a site administrator (or an alternate, within D1a) | The same checks, plus the approval | Deploy and the post-deploy check. The approval is written to the audit trail. |

**Field tiers.** The table below is normative. Axiom ships it verbatim as data,
`axiom/site/config_tiers.toml`, and RIVET and the lint read that file and never a copy. A
pack declares tiers for its own `[ext.<pack>]` fields through `SiteExtension.tiers`, and
the default for a pack field is notable. **A field with no declared tier is governed**,
so the rule fails closed.

| Field | Tier |
|---|---|
| `name`, `brand.*` (name, accent, logo, tagline, support address), guide and prompt text | routine |
| Schedules and maintenance windows (when, not what) | routine |
| Channel-map edits to existing channels (label, unit, description, chart hints) | routine |
| Expectation parameters (ranges, freshness windows, alert thresholds) | routine |
| An existing connector's folder or path | routine |
| Parameters of an existing pipeline step | routine |
| Platform pins moved forward to a release that CI has passed | routine |
| A new connector, or removing one | notable |
| A new pipeline step, removing one, or reordering steps | notable |
| A new channel in a channel map | notable |
| `licence.promote_default`, within the bounds the site policy allows | notable |
| `profile` and `nodes` (adding a node or a function), other than the medallion holder | notable |
| `ext.<pack>.*`, unless the pack declares a tier | notable |
| `slug` rename; `uuid` is immutable and refused | governed |
| `org`, `alternates` | governed |
| `succession_wait_hours` (default 72, floor 24; D1a) | governed |
| `visibility` | governed |
| Program consent and withdrawal; accepting new terms | governed |
| `licence.utility_license_default`, `licence.utility_licenses_allowed`, `promote_default` outside policy | governed |
| `federation.*` (edges, `accept_identity_from`) | governed |
| `policy.*` (site rules, autonomy floor, classification ceiling, agent policy, maintenance level) | governed |
| `tenancy.*`, and which node is the medallion holder | governed |
| Any secret-adjacent field: key or token references, vault paths, issued key ids | governed |
| Marking a dataset `publishable`, `publish`, and `publication_target` (D9) | governed |
| The D8a timing settings, except the reminder cadence | governed |
| `approval_reminder_days` | notable |
| Renewing an issued key or grant (same scopes, nothing widened; KEEP, D8a) | notable |
| Any field not listed | governed |

**Post-deploy check and automatic revert.** After every deploy, RIVET runs the ADR-178
checklist and the site's health probes. If a check that passed before the change now
fails, RIVET opens and merges the revert at the same tier, redeploys and notifies the
maintainer. It does not retry the change.

**Emergency override.** A site administrator can apply a change live with
`axi site config set --emergency --reason <text>`. It is audited at once, and RIVET opens
the matching pull request straight away so that git catches up. If that pull request is
not merged within `override_reconcile_hours`, RIVET flags the drift to the site's
administrators, reminds them on the D8a backoff, and later escalates to the alternates.
The live state stays as it is while the request waits. An override never bypasses the
post-deploy check.

### D8a. Defaults for inattentive humans (decided, 2026-10-28)

The owner's rule: **make settings generous, and assume people are inattentive.** Every
timing and approval default that this ADR touches, and that ADR-205 and ADR-208 hand to
it, follows these rules:

- **Requests that wait for a person never expire on their own.** A pending approval does
  not quietly turn into a denial or an approval. It waits. RIVET sends reminders on a
  backoff and, after a generous period, escalates to the alternates.
- **Routine and notable changes never wait for a person** (D8).
- **When nobody responds, the safe outcome is the current state.** Nothing is widened,
  nothing is deleted, and a governed action is never approved automatically.
- **Expiries warn well ahead,** and each warning offers a one-action renewal.

**Two agents own these settings.**

- **RIVET (the `release` builtin) owns the change pipeline:** config pull requests and
  their tiered merges (D8), approval reminders and escalation, the notable-tier revert
  window, override reconciliation, and post-deploy reverts.
- **KEEP (the `vault` builtin, ADR-055) owns every expiry,** whether the node holds the
  credential or issued it, on its hourly heartbeat. Today KEEP watches only held secrets.
  `vault/skills/renew.py` runs an hourly expiry-rotation pass with a 3-day horizon; it
  rotates only credentials that are unattended and opted in, escalates the rest, and
  always states `expires_at`. `secrets.audit` uses a 14-day horizon. Issued credentials
  (the gate's API keys and ADR-205 grants) carry an expiry, but nothing watches it.

**The KEEP extension this ADR needs.** `vault.audit` and `renew` learn to read issued
credentials from the gate's key store and from the grant store. For an issued
credential, KEEP warns at `expiry_warning_days`. Each warning goes first to the holder,
then to the site administrator, then to the alternates, and each offers a one-action
renewal.

**Issued keys and grants never renew themselves.** Self-renewal would extend access
silently. Renewal is a notable-tier action, done with one action by the holder or a site
administrator, and it renews the same scopes and `where` with nothing widened. A held
secret keeps KEEP's existing rotation rules.

Every setting below is per-site config. Its tier is governed, except the reminder
cadence, which is notable. A value outside its floor or ceiling is refused by
`axi site validate`.

| Setting | Owner | Default | Floor or ceiling | What happens when nobody acts |
|---|---|---|---|---|
| `approval_reminder_days` | RIVET | 1, 3, 7, then weekly | Weekly is the slowest allowed cadence (the reminders cannot be switched off) | The request stays pending |
| `approval_escalate_days` (escalation to alternates) | RIVET | 14 | Floor 7; there must be at least one escalation | Alternates are asked; the request still stays pending |
| Governed-change approval expiry | RIVET | none (waits indefinitely) | Cannot be set to auto-approve | The current state is kept |
| `notable_revert_days` (one-action revert of a notable change) | RIVET | 30 | Floor 7 | The change stays |
| `succession_wait_hours` (D1a) | RIVET (it runs the alternate's request) | 72 | Floor 24 | Succession proceeds only if nobody objected |
| `override_reconcile_hours` (D8) | RIVET | 72, then reminders | Floor 24 | The live state stays and the drift is flagged |
| `expiry_warning_days` (issued gate keys and ADR-205 grants) | KEEP | 30, 7, 1, to the holder, then the site administrator, then the alternates | At least one warning at 7 days or more | The key or grant expires as it was issued; it is never renewed automatically. Renewal stays one action. |
| Held-secret rotation horizon (`renew`) | KEEP | 3 days, hourly pass | Never shorter than several heartbeats | Unattended, opted-in secrets rotate; the rest are escalated |
| Held-secret audit horizon (`secrets.audit`) | KEEP | 14 days | Floor 7 | The secret is reported as expiring |
| Publish request (D9) | RIVET | queued until someone acts | Cannot expire | Nothing is published |
| Program-terms consent (ADR-205 D2) | RIVET | waits, with reminders | Cannot be set to auto-consent | The member stays on its last consented terms |

The post-deploy revert (D8, RIVET) is the one action that does not wait for a person,
because it restores the last good state.

### D9. Public data is published as snapshots; live sharing stays federation (proposed)

| Option | What | Assessment |
|---|---|---|
| A. Anonymous reads from the site node | Write a `viewer@user:*` tuple and let anyone query the node | **Not adopted.** Many site nodes are on private networks and cannot be reached anyway. Anonymous load costs money, invites abuse and competes with operations. Live operations data is security-sensitive, and one mistaken tuple exposes all of it. |
| B. Published snapshots | A site marks a gold dataset or view `publishable`. `axi site publish` (governed tier) produces a frozen, versioned, licensed snapshot with provenance and checksums, deposited at the site's publication target and citable by DOI | **Recommended** for everything public. The node never serves anyone anonymous. |
| C. Federation and programs | Live sharing with known partners under ADR-205 terms | **Adopted** (already the design) for everything live |
| D. A dedicated public edge node | A separate node holding only replicated public-classification data, serving anonymous reads behind rate limits | **Deferred.** Revisit only if a sponsor requires live public access, and then as its own ADR |

The recommendation is B plus C.

**The publication-target seam (proposed).** A site's config field `publication_target`
(governed) names where its snapshots go. It defaults to the owning org's default, the
same way the licence default is inherited (ADR-208 D10). One command, `axi site publish`,
works with every target, and each target is an adapter registered through the
entry-point group `axiom.publication_targets`. The first adapters:

| Target adapter | Holds the bytes | Mints the DOI |
|---|---|---|
| An institutional data repository | yes | yes, through the repository's registration agency |
| A general-purpose open repository (an open deposit API) | yes, up to its file-size limit | yes |
| A national-lab DOI service | no: it registers the DOI and points at bytes held elsewhere | yes |
| Object storage or an HPC archive | yes, for large bytes | no: it is linked from the DOI record of another target |

A target may be a pair, for example a DOI service plus an archive. The adapter declares
its limits (file size, total size, formats), and `publish` checks them before it asks
anyone to approve.

**The standard snapshot.** Every target receives the same package:

- the data as Parquet;
- a data dictionary (column, type, unit, description, classification);
- provenance: the site, the gold view, the utility versions (ADR-208), the time range,
  the conform lineage and the rebuild identity (ADR-206);
- checksums for every file;
- the licence, as an explicit SPDX identifier, because an inherited default is not enough
  (ADR-208 D10);
- a README.

**The flow.**

1. Someone marks a dataset or view `publishable`. This is governed.
2. `axi site publish <dataset> --version <v>` builds the snapshot and queues the request.
   It stays queued until someone acts (D8a).
3. A site administrator approves it. The approval includes a **sensitivity review**: a
   checklist recorded with the approval, because operations data can be sensitive to
   security in ways that go beyond export control. The classification must be `public`.
4. The adapter deposits the snapshot and records the DOI.

A published version is frozen. Publishing again creates a new version under the same
concept DOI, and it names the version it supersedes. Nothing is changed in place.

### D10. Migration: expand, shim, move, contract

Every step is expand-only for one release and keeps every existing path working.

**M1. Axiom: the record and the resolver.** `axiom.site` with the record (`uuid` plus `slug`), its schema, the D8 tier file,
`active()`, `served()`, `home()`, the loaded alias table, and the default site at setup.
`axi site show | whoami | bind | redeem | use | unbind` writes the same
`site-bindings.json` format. The environment variables stay as overrides, and a mismatch
between a variable and the record is logged with both values. The direct readers move to
the resolver.

**M2. Axiom: the pattern.** The site-extension seam, the tiered site-data search, brand
from the record (the `AXIOM_BRAND_*` variables still override), `axi site init | validate |
lint | deploy | config` ported from the consumer, RIVET's D8 merge process, KEEP's watch over issued credentials (D8a), the reusable `site-ci` workflow, the D7 model
loaded and tested against the D2 matrix.

**M2a. Authorization tuples: `platform` to `org`.** This is a one-time, idempotent
rewrite, and it runs before the new model is written to a store:

- `platform:<p>#admin@<subject>` becomes `org:<o>#admin@<subject>`;
- `site:<s>#parent@platform:<p>` becomes `site:<s>#owner_org@org:<o>`.

It takes an explicit `<p>=<o>` mapping. It refuses any platform it cannot map, and it
writes the new tuples before deleting the old ones, so there is never a moment with
neither. The model's test fixtures are rewritten in the same change.

**Live state, verified read-only on 2026-10-28.** The production node's OpenFGA store
(Postgres database `openfga`, about 8 MB) holds 1 store, 1 authorization model,
**0 tuples** and 0 changelog rows. The rewrite is therefore a no-op on that node today.
It stays in the migration so that any store written before M2 lands is still converted.

**M3. Domain consumer: shims.** Its binding, resolver, brand, redeem, deploy and scaffold
modules re-export or forward to `axiom.site` with a deprecation notice; its `site`
command forwards to `axi site`. It registers one site extension for its domain fields.
Its own extra resolvers are deleted, not shimmed. It raises its Axiom floor to the M2
release.

**M4. Site repositories.** Each receives a `site.toml` in the D6 shape (old `[site]`
domain keys accepted with a notice for one release), deletes the managed files SR002
names, adopts `site-ci`, and moves its pins forward. The host site repository's platform
code is triaged module by module into Axiom, the consumer or the site, each move its own
change; this is the long pole and is not on the critical path for M1 to M3.

**M5. Contract.** Shims and override variables are removed two releases after their
counters read zero on every deployed node.

Pins move forward only. A node is upgraded through its canary path with its rollback
slot kept; no step requires a node to be down, and no step changes the binding file
format, a site id in a row, or a key.

## Consequences

- One answer to "which site, whose, who can see it" for every surface, every pack and
  every repository. ADR-208's licence and promotion defaults have a home.
- A personal install is a first-class site, so personal state (ADR-207's local Postgres)
  belongs to something, and moving from personal to org or shared is a visibility change,
  not a reinstall.
- The domain consumer loses its site package except one extension; partner repositories
  shrink to configuration and content.
- New state: the declared record and its schema, the runtime registry (expand-only,
  ADR-182), and the visibility and program tuples.
- The cost lands mostly in the host site repository, whose platform code needs triage.
- A site with one owning org cannot express joint ownership directly; alternates cover
  succession and emergencies, and a consortium is a program whose hub is a site owned by
  one of its members.
- Most site configuration changes merge without a person, so the field-tier table is
  load-bearing. A field filed in the wrong tier merges at the wrong level of attention,
  which is why an unlisted field is governed.
- Public data costs a publish step and storage. In exchange, no site node ever takes
  anonymous traffic.

## Alternatives considered

- **Leave site in the domain consumer and read it from Axiom.** This is today's state,
  and it is why Axiom has many readers and no writer.
- **Make a program a kind of site.** It would let a program own data, which reopens the
  union problem and blurs who is accountable. A program stays an agreement.
- **Site per node.** Contradicts hosted tenants and multi-node sites, both already real.
- **Put visibility in the record only, not in tuples.** Then discovery would be decided
  in two places. Tuples keep one decision point; the record states the intent and the
  skill writes it.

## Open questions for the owner

Resolved on 2026-10-28:

- **Ownership:** one owning org with alternates (decided; D1a).
- **Command:** `axi site`, with the `axi sites` alias (decided; D5).
- **Default slug and addressing:** proposed in D1.
- **Config editing:** proposed in D8.
- **Public data:** proposed in D9, including the publication-target seam.
- **Defaults for inattentive humans:** decided (D8a).
- **FGA types:** `platform` is renamed `org`, and `host` and `node` are added (decided; D7).
- **Succession wait:** a default of 72 hours with a floor of 24, set per site (decided; D1a).

Still open:

1. The emergency-override reconciliation window: it is now in the D8a table (72 hours,
   floor 24). Confirm it.
2. For each site: the data-sharing terms of its funding agreements, and whether they
   require or forbid a particular publication target.
3. Each target repository's file-size limits, and when to split a snapshot or hand its
   bytes to an archive.
4. Whether a curator is available at each target, and how long its review takes.
