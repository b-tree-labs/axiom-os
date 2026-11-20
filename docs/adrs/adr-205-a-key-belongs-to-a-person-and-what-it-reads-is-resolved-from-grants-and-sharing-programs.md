# ADR-205: A key belongs to a person, and what it reads is resolved from grants and sharing programs

**Status:** Proposed (2026-10-27). Draft for the platform owner's review; nothing here is built.
**Date:** 2026-10-27  •  **Deciders:** platform owner

**Related:** ADR-151 (roles separate governing from reading), ADR-153 (a role is granted
at a site), ADR-130 §5 (multi-site membership; amended here for API keys), ADR-103
(directory providers; §10 revocations ahead of grants, §12 degrade toward less
authority), ADR-114 (MCP identity gate), ADR-084 (actor context), ADR-128 (medallion
tier boundaries), ADR-157 (serving budgets), ADR-178 (a tenant site is one record),
ADR-203 (a data kit is promoted as data), ADR-052 (schema per extension). Spec:
`spec-security.md` §4.5.

## Context

### What exists today

A key carries a site, and two rules decide what that site means.

- **Minting.** `mint_api_key(principal, scopes, name, site)` in
  `src/axiom/webauth/api_keys.py` writes a record with a `site` field, and
  `bind_principal_to_site` folds the site into the handle (`@svc` plus `site-a`
  becomes `@svc:site-a`). Self-service minting
  (`webgate/api/self_key_route.py`, 0.70.9, commit `0ba603c4`) requires every key to
  carry a site: the node's home site (`AXIOM_SITE`) or one listed by a role's
  `sites = [...]` option in the roles file (`webgate/role_bundles.py`,
  `RoleBundle.sites`, `registry.sites_for(roles)`). A person who works with three
  sites mints three keys.
- **Binding at the door.** The HTTP authz hook reads `site` from the verified
  credential's claims (`http/authz_hook.py`), and the middleware binds it into a
  context variable for the request (`bind_caller_site`, `http/middleware.py`).
- **Rule A: a site-bound key sees one site.** `tenant_scope()` in
  `src/axiom/infra/site_authority.py` returns the credential's site when that site is
  not a home site, and `authorize_site` / `_hold_to_tenant` refuse a read naming any
  other. The gold reads (`data_platform/skills/gold.py`, `_site_scope`) apply the
  scope as a predicate the caller cannot remove (`gold_query.scope_clause`), and it
  fails closed on a table with no site column.
- **Rule B: a home-site key sees all.** `tenant_scope()` returns `None` for a
  credential bound to a home site (`AXIOM_SITE`, the node's binding, or
  `AXIOM_HOME_SITES`), the same `None` an operator at the terminal gets. So a key for
  the host's own site reads every hosted site in gold.
- **Kits already refuse Rule B.** 0.70.11 (commit `db3744cc`) holds the promoted-kit
  reads (`read_target.KIT_READS`: `data.kit_verbs`, `data.kit_verb`,
  `data.kit_charts`, `data.kit_automations`) to the key's site, home site included
  (`kit_promotion._caller`, `_held`), because Rule B let a contributor's home key
  list every site's verbs. Writes (`kit/host.submit`) refuse a bundle for a site
  other than the caller's, and a site's objects run as that site's database role
  (`kit_<site>`, `SET LOCAL ROLE`) with row-level security on `silver.signals`
  admitting only `site = <site>`.
- **Grants.** ADR-153 makes a role binding a triple `(principal, role, where)` with
  `where` a site or `*`, scopes composing by union and autonomy by minimum, and lets a
  site withdraw a `*` binding. ADR-130 §5 says memberships link under one identity
  and never union, that each credential names exactly one active site, and that a
  cross-site view is an explicit fan-out with per-site marks.
- **Federation, cohorts, peers.** ADR-020/022 define peers and signed membership
  manifests between nodes; ADR-027's cohort registry addresses memory fragments. None
  of them models sites on one node agreeing to share data with each other.

### Why that is not enough

The two rules are both wrong at the edges, in opposite directions. Rule A forces a
person in a multi-site collaboration to hold one key per site, which is a usability
failure and multiplies the secrets to rotate and revoke. Rule B gives the host's
people every hosted site by accident of where their key was minted, which is a
silent union of exactly the kind ADR-130 §5 forbids; the kit fix in 0.70.11 is a
local patch over it, not a rule.

The owner's direction: sites on one platform sometimes trust each other (several
institutions on one large project will share all their data because of the project)
and sometimes do not know each other at all and must stay silos. A key is minted
**once**, bound to the **person**; what it unlocks is **dynamic**, decided by
configuration that someone with authority over the sites approves; a site in no
sharing arrangement stays a silo.

## Options considered

### 1. Per-site keys (today)

One key per (person, site). Simple to reason about, and Rule A is already enforced.
It lost because the cost lands on the people the platform most wants to serve (a
collaborator on a five-site project carries five keys), every access change is a
mint or a revoke of a secret, and it does nothing about Rule B.

### 2. Person keys with static role sites

A key binds to the person; the sites it reads are the role's `sites = [...]` list
from the roles file, read at mint time or at request time. It removes the key per
site, but the list is node configuration that one operator edits: it cannot express
that each site agreed, it has no expiry, it has no record of why a site appears, and
widening a role widens every holder at once. It turns the roles file into the
sharing agreement without any of an agreement's properties.

### 3. Person keys with sharing programs (recommended)

A key binds to the person. On each request the node resolves the person's grants:
grants at a site, and grants on a **sharing program** (a named agreement among
member sites, each of which consented through its own administrator). The answer is
a set of sites, each with the role and the limits that allowed it. Access changes
are grant and membership changes, effective on the next request, with no key
re-issued. This is the option the rest of this ADR specifies.

## Decision

**A key identifies a person, never a site. What a request may read is resolved on
that request from the person's grants: grants at a site, and grants on a sharing
program whose member sites each consented. A site in no program is visible only to
the people granted at it. Every write names exactly one site and runs as that site.**

Vocabulary: the concept is a **sharing program** ("program" in prose below). The CLI
noun is `sharing`, because `axi program` is already the program tracker's noun.

### D1. A key resolves to a principal and nothing else

A new key record carries `principal` (the person's handle with no `:context`, keyed on
the immutable directory subject per ADR-103 §11), `scopes`, `expires_at` and
`revoked_at`. It carries no `site`.

A key may carry an optional **`held_to`** list of sites. It only narrows: the
request's access set is intersected with it, so a person can mint a key for one
script that touches one site. It never widens. Legacy keys map their `site` onto
`held_to` (D10).

Scopes stay what they are (which mounts and verbs). Sites are not a scope.

### D2. A program is an agreement among member sites

A program has a name, a steward set, a state (`draft`, `active`, `closed`) and
versioned **terms**:

| Term | Meaning | Default |
|---|---|---|
| `tiers` | medallion tiers a program grant may read (ADR-128) | `[gold]` |
| `classification_ceiling` | highest classification a program read may return | `internal` |
| `domains` | data domains (catalog domains, table prefixes) shared; empty means all the member shares | all |
| `max_role` | highest role a program grant confers | `viewer` |

A site is a **member** only after its own administrator **consents** to a named terms
version. Nobody enrols a site they do not administer. Widening the terms (a new tier,
a higher ceiling, more domains) creates a new version that each member must consent
to again; until it does, that member is read under the last version it consented to.
Narrowing applies to every member at once, because less sharing needs nobody's
agreement.

A member may **withdraw** at any time; its data leaves the program on the next
request.

### D3. A grant gives a person a role at a site or on a program

A grant is ADR-153's triple with a wider `where` and a lifetime:
`(principal, role, where, expires_at)`, where `where` is `site:<id>`,
`sharing:<id>`, or `*` (ADR-153's node binding, unchanged). The principal may be a
directory group (ADR-103), which resolves through that group's membership projection.

- A **site grant** is issued by that site's administrator. It may carry any ADR-151
  role.
- A **program grant** is issued by a program steward, or by any member site's
  administrator. It reads every active member site, each clipped to the terms that
  member consented to. It is **read-only by construction**: `max_role` is capped at
  `viewer`, and writing at a site always needs a site grant there.
- A **steward** is appointed by the member sites: each member's consent names whether
  it accepts the program's stewards granting access to its data. A member that does
  not accept stewards is reachable only through grants its own administrator issues
  on the program.
- Every grant may expire. An expired grant is ignored at resolution; nothing has to
  run for it to stop working.

Scopes compose by union and autonomy by minimum, exactly as ADR-153 §2 and §3 say. A
site that withdrew a `*` binding (ADR-153 §4) is not reached by it here either.

### D4. A silo stays a silo

A site in no active program is read only by principals with a grant at that site (or
a `*` binding it has not withdrawn). To everyone else it does not exist: catalog and
site listings omit it, and a request naming it is refused with the same words as a
site that does not exist, so its name is not disclosed by the refusal.

### D5. Request-time resolution

The authz hook verifies the key and binds a **principal**, not a site. The first read
on the request resolves an **access set**:

```
AccessSet = { site -> Access(role, via, tiers, ceiling, domains) }
via       = site-grant:<id> | node-grant:<id> | sharing:<program>/<grant-id>@terms-v<n>
```

1. Collect the principal's live grants (not revoked, not expired), including grants to
   directory groups the principal is in.
2. Site grants and `*` grants (minus withdrawn sites) give their sites with no clip.
3. Each program grant gives every **active** member of that program, clipped to the
   terms version that member consented to, and only if that member accepts the
   grant's issuer (steward or its own administrator).
4. Where a site arrives by more than one route, roles union and limits take the
   widest route that allows the read (a site grant beats a program clip). The audit
   names the route actually used (D9).
5. Intersect with the key's `held_to`, if any.

`tenant_scope()` is replaced by `access_scope()`, which returns the `AccessSet`, or
`None` only where `tenant_scope()` returns `None` for a missing credential (an
operator at the terminal, a stdio session). A home site no longer means "every site".

**Cost.** The store is small (grants per principal, members per program, in the tens)
and lives in the gate's schema (ADR-052). Resolution is a few indexed reads and an
in-memory merge, done once per request.

**Caching and the revocation bound.** The store keeps one **access epoch**, a
counter every grant, membership, terms or key change increments in the same
transaction. Each process caches access sets keyed by `(principal, key_id, epoch)`,
expiring at the earliest `expires_at` in the set. Each request reads the epoch
(one row, primary key) before using a cached set. So:

- **A revocation, withdrawal or narrowing is effective on the next request that
  starts after it commits, on every process of the node.** A request already running
  finishes under the set it started with, bounded by the ADR-157 per-call budget.
- Grants to directory groups inherit ADR-103's freshness: removals take effect through
  its recently-revoked set on the next resolution (§10), additions on its projection
  cadence.
- If the store cannot be read, resolution fails closed (ADR-103 §12): the request is
  refused with a 503 naming the store, never served from a stale set.

### D6. Reads across sites are one query per site, unioned in the application

The node still runs SQL as a site's role. A read that spans the access set is a
**fan-out**: for each site in the set that the request asks for (all of them, for a
list or catalog verb), the node runs the query in its own transaction under that
site's read role, with `scope_clause` holding it to `site = <that site>`, and unions
the rows in the application, each row and each result marked with its site. This is
the explicit fan-out ADR-130 §5 already requires for a cross-site view.

We do not grant a principal a **role set** (one database role holding several site
roles, or a row-level policy over `site = ANY(current_setting(...))`). Kit verbs are
caller-authored SQL, and a policy that reads a session setting is one `SET` away from
being argued with. One query per site keeps each policy a literal `site = <id>` on a
role that can see one site, which is what `kit/host` already proves from the catalog
at apply time. The cost is linear in the number of sites; each per-site query counts
against the ADR-157 budget, and a request may span at most a configured number of
sites (default 16) before it is refused and asked to narrow.

The gold reads that run today as the serving login with a predicate move to per-site
read roles as part of this work, so a cross-site read is held by the database, not
only by the predicate.

Tier and term limits are enforced at the same seam: a program-routed site sub-query
on a tier, domain or classification outside its terms is refused for that site, and
the result says which site was withheld and why, rather than returning an empty set
that reads as "no data".

### D7. Writes name exactly one site

Any write, including a kit submission or promotion (ADR-203), names exactly one
target site. It requires a **site grant** at that site with a role that writes
(`creator`, `operator` or `owner`); a program grant never suffices, and a request that
names no site or several is refused. The promotion's owner is recorded as a
**principal on a site** (`@person` plus `site-a`), replacing the `@site:<site>`
placeholder `kit_submit` uses today. The apply and the served verbs keep running as
`kit_<site>` under row-level security, unchanged.

Kit reads (`KIT_READS`) follow D6: a person reads the kit objects of every site in
their access set, each marked; running one verb names its site, because a verb
belongs to one site.

### D8. Admin flows and verbs

Verbs are skill functions (ADR-056), projected to CLI and MCP, and audited through
`axiom.infra.audit_trail`. Mutating verbs are never auto-approved for an agent.

| Flow | CLI | MCP | Who may |
|---|---|---|---|
| Create a program and its first terms | `axi sharing create program-x --tiers gold --ceiling internal [--domains d1,d2]` | `axiom_sharing__create` | any site administrator; the program starts `draft` with no members |
| Invite a site | `axi sharing invite program-x site-b` | `axiom_sharing__invite` | a steward or a member's administrator; creates an `invited` membership, grants nothing |
| A site consents | `axi sharing consent program-x --site site-b --terms v1 [--accept-stewards]` | `axiom_sharing__consent` | an administrator **of site-b** only |
| Change terms | `axi sharing terms program-x --tiers gold,silver` | `axiom_sharing__terms` | a steward; widening waits on each member's re-consent |
| A site withdraws | `axi sharing withdraw program-x --site site-b` | `axiom_sharing__withdraw` | an administrator of site-b |
| Close a program | `axi sharing close program-x` | `axiom_sharing__close` | a steward; every program grant stops resolving |
| See a program | `axi sharing show program-x`, `axi sharing list` | `axiom_sharing__show`, `axiom_sharing__list` | members' people (read-only) |
| Grant a person | `axi gate grant @person --role viewer --at sharing:program-x [--expires 2027-06-30]` or `--at site:site-a` | `axiom_gate__grant` | the site's administrator, or a steward the members accept |
| Revoke a grant | `axi gate revoke-grant <grant-id>` | `axiom_gate__revoke_grant` | whoever could issue it, and the person it names |
| Explain access | `axi gate access @person [--site site-b]` | `axiom_gate__access` | the person, and administrators of the sites it lists |

`axi gate access` prints the resolved access set with each site's route and terms,
the same object the resolver uses, so "why can I see this" and "why can't I" have one
answer. `axi whoami` shows the caller's own.

### D9. Audit

Every read whose route is a program, or that spans more than one site, writes a
receipt: principal, key id, request id, verb, each site read, and for each site the
route (`grant_id`, and for a program `program`, membership id and terms version),
rows returned and any site withheld. The receipt is projected into each **affected
site's** audit view (`axi audit list --site site-b`), so a member sees every read of
its data that came through a program, and sees nothing about reads of other sites.
Consents, withdrawals, terms changes, grants and revocations are audited with the
acting principal and the role it held where it acted.

### D10. Migration from site-bound keys

No existing key stops authenticating. During a **compatibility period** (two minor
releases, at least 60 days), the resolver synthesizes grants so each legacy key sees
exactly what it sees today:

- **Rule A keys** (a `site` that is not a home site): `held_to = [site]`, plus a
  synthesized site grant `(principal, <role from the key's scopes>, site)` labelled
  `legacy-key:<key_id>`. The `held_to` part is permanent; the key keeps working,
  held to one site, for as long as it lives.
- **Rule B keys** (a home site): a synthesized `*` read grant labelled
  `legacy-home:<key_id>`, reproducing "sees every site".
- **Role `sites = [...]`**: read as a template that offers site grants to the role's
  holders; self-service minting stops asking for a site and mints person-bound keys.

Phases:

1. **Shadow.** The resolver runs beside `tenant_scope()` and records any
   disagreement; by construction there should be none, so each one is a bug.
2. **Enforce.** `access_scope()` replaces `tenant_scope()` at every caller (gold,
   medallion, resolvers, chat retrieval, the federation endpoint, kit promotion).
   New keys are person-bound.
3. **Convert.** `axi gate migrate-keys --plan` lists every synthesized grant and who
   must approve it; `--apply` turns the approved ones into real grants. Synthesized
   `legacy-home` grants are not converted automatically: each site's administrator
   decides whether the host's people keep reading it.
4. **End.** Synthesized grants stop resolving. A legacy home key without converted
   grants reads only its home site, which is ADR-153's narrower reading, announced
   in the release notes and by `axi gate migrate-keys --plan` beforehand.

### D11. Scope

Programs are among sites hosted on **one node**. A program spanning nodes needs the
federation edge to carry the membership and consent (ADR-022 manifests) and is a
later decision; nothing here widens what a peer can assert (ADR-151 §4, ADR-153).

## Tests that prove it

Each is a test the build ships with; the first five are the owner's.

1. **A silo stays a silo.** site-c is in no program; a person granted on program-x
   (site-a, site-b) and at site-a reads site-a and site-b, and a read naming site-c is
   refused with the same text as an unknown site; catalog listings omit site-c; a
   kit-verb list omits site-c's verbs.
2. **A revoke is effective on the next request.** Two server processes; a cached
   access set in each; revoke the grant; the next request on either process is
   refused, with no sleep in the test. Same for a member withdrawal and for a terms
   narrowing.
3. **A program grant reads all member sites.** A gold aggregate with no site named
   returns rows marked site-a and site-b, the receipt names program-x and each terms
   version, and each per-site query ran as that site's role (asserted from
   `pg_stat_statements` or the role recorded per sub-query).
4. **A write cannot target a site the person cannot write.** A person with a
   program grant covering site-b, and a site grant at site-a, submits a kit for
   site-b: refused; for site-a: accepted, owned by `@person` on site-a, applied as
   `kit_site_a`. A submission naming no site or two sites is refused.
5. **Consent cannot be forged by one site for another.** site-a's administrator calls
   `consent program-x --site site-b`: refused. A membership row inserted with a
   consent from a principal who held no administrator binding at site-b at that time
   is not counted by the resolver. A `*` administrator's consent is refused (see open
   question 1).
6. **Terms bind.** program-x shares gold only: a silver read routed through it is
   refused for that site and named in the result; a classification above the ceiling
   is withheld; widening the terms changes nothing for a member until it re-consents.
7. **Expiry needs no job.** A grant with `expires_at` one second ahead stops resolving
   after it passes with no process running.
8. **`held_to` only narrows.** A key held to site-a, for a person granted on
   program-x, reads site-a only.
9. **Compatibility parity.** For a fixture of legacy keys (Rule A, Rule B, role
   `sites`), the shadow resolver and `tenant_scope()` agree on every read in the
   existing tenancy suites (`test_a_site_bound_credential_reads_only_its_site_in_gold`,
   `test_rag_tenant_isolation`, `test_a_promoted_kit_is_used_from_a_laptop`).
10. **Audit reaches each affected site and only it.** A program read of site-a and
    site-b shows in both sites' audit views and not in site-c's.
11. **The store failing fails closed.** With the grant store unreadable, a read is
    refused with a 503, never served from cache.
12. **A fan-out is bounded.** A request over more sites than the limit is refused and
    asked to narrow; each sub-query is counted against the ADR-157 budget.

## Consequences

**Easier.** A person holds one key for every site they work with. Access changes are
grant and membership changes, effective on the next request, with no secret
re-issued or redistributed. Rule B's accidental "home key reads everything" goes
away, and the 0.70.11 kit exception stops being an exception. A site can answer "who
read our data through a program, and under what terms" from its own audit view.

**Harder.** Every read now depends on the grant store, so its availability becomes a
read dependency (mitigated by caching on the epoch and failing closed). A cross-site
read costs one query per site. Administrators gain a consent step that has to be
explained in words a site's administrator reads before agreeing, as ADR-153 already
requires for invitations.

**Committed to.** One resolver (`access_scope()`) that every site-held read goes
through; the access epoch on every mutation; per-site read roles for gold; a
`sharing` extension (or a section of the gate) owning programs, memberships and
terms; receipts per affected site; the compatibility period and its end date in the
release notes.

**Amends.** ADR-130 §5's "one active site per credential" becomes "one person per
credential; every write names one site; every cross-site read is an explicit,
site-marked fan-out". ADR-153 gains `sharing:<id>` as a `where` and expiry on every
grant; its composition rules stand.

## Open questions for the owner

1. **Who may consent for a hosted site?** ADR-153 lets the hosting party administer
   every site through a `*` binding. If that binding may consent, the host can enrol
   any hosted site in a program, which is the forgery the owner's direction forbids.
   This draft requires consent from an administrator bound at that site, and so a
   site with no administrator of its own cannot join a program. Is that right, or may
   a `*` administrator consent on a site's documented instruction?
2. **What happens to home-site keys at the end of the compatibility period?** The
   draft narrows them to their home site unless each hosted site approves a converted
   grant. That removes access the host's people use today. The alternative keeps a
   converted `*` read grant by default, which keeps Rule B's breadth under a new
   name.
3. **May a program ever confer more than reading?** The draft caps program grants at
   `viewer` and requires a site grant to write. A large project may want contributors
   to promote kits at every member site without each site granting them separately;
   allowing it would make the steward a writer at sites it does not administer.
4. Smaller: the CLI noun (`sharing`, since `program` is taken); the default fan-out
   limit; whether a member may see the list of other members' people holding program
   grants.
