# ADR-103: Directory Providers — group and membership resolution as a pluggable seam

**Status:** Accepted (2026-08-26) — proposed 2026-08-18; built and merged in four phases, see *Implementation status* below
**Deciders:** Benjamin Booth
**Related:** ADR-075 (SSO/OIDC delegated auth — the `auth` RP registry this
mirrors), ADR-084 (ActorContext — `roles`/`attributes` are what this populates),
ADR-083 (OpenFGA — the substrate whose tuples this writes), ADR-086
(authenticated delegation — requires *live* permissions, not token-time ones),
ADR-022 (federation identity roots + membership separation — why providers must
be plural), ADR-094 (permission-aware retrieval — consumes the identity→tier
grant), ADR-055 (GUARD — the decision point), ADR-020 (handle grammar),
ADR-026 (ownership model — four rights as relations).

---

## Context

Axiom can authenticate a person and it can authorize an action, but nothing
connects the two.

**Authentication is solved.** ADR-075's `auth` extension ships an IdP adapter
registry (`entra(tenant)`, `google()`, generic OIDC discovery), device-code and
PKCE flows, and a token store keyed `(provider, user, scopes)`. A sign-in yields
a verified `id_token` and a principal.

**Authorization is solved.** ADR-083 adopted OpenFGA as the substrate GUARD
calls, with RBAC mapped to roles-as-relations and ReBAC to relationship tuples.
ADR-084 threads an `ActorContext` carrying `roles` and `attributes` into every
`ActionEnvelope`.

**Membership is not solved at all.** OpenFGA is a storage-backed relationship
graph: it answers questions about tuples it has been given, and it discovers
nothing on its own. Today no component writes those tuples, and
`ActorContext.roles` is populated only insofar as an IdP happens to place group
claims in a token. A survey of the code found no `memberOf`, no SCIM, no group
provider, and no `roles` sourcing anywhere in the `auth` extension; a survey of
all 101 existing ADRs found no decision covering directory or group
provisioning. The substrate is real and the graph is empty, so every `Check`
returns `ABSTAIN` and authorization falls through to the deterministic floor.

This blocks concrete work: onboarding a department by group membership, the
identity→allowed-tiers grant table ADR-094 depends on, and any
resource-scoped authorization finer than a flat role.

### Why token claims alone are not the answer

The tempting shortcut — read groups from the `id_token` and stop — fails on four
counts, each independently disqualifying:

1. **Claims overflow.** Entra emits a `groups` claim only when configured, and
   past the token size limit it replaces the list with a `_claim_names` /
   `_claim_sources` pointer to a Graph endpoint. Any implementation must be able
   to call the directory anyway, so a claims-only design has a directory client
   hidden inside it regardless.
2. **Claims go stale.** Removing someone from a group does not invalidate their
   outstanding access token. ADR-086 makes effective authority the intersection
   with the delegator's *live* permissions precisely so that revoking a human
   starves every capability derived from them — a guarantee that a token-time
   snapshot cannot deliver.
3. **Not every membership source is an IdP.** An HR system pushing SCIM, an AD
   group, a GitHub team, or a flat file on an air-gapped node are all legitimate
   sources that never appear in an OIDC token.
4. **Federation membership belongs to no single tenant.** ADR-022 commits Axiom
   to academic (InCommon/eduGAIN), government (PIV/CAC/FICAM), and industry
   (consortium PKI) anchoring authorities. A seam shaped around one tenant's
   directory contradicts a decision already made.

## Decision

**Introduce a `directory` capability: a provider registry for group and
membership resolution, mirroring `auth` (ADR-075) and `secrets`.** It is the
missing edge between an authenticated principal and an authorization graph.

### 1. The port

A `DirectoryProvider` Protocol, deliberately small, with declared capabilities
rather than a fat interface every adapter must fake:

```python
class DirectoryProvider(Protocol):
    def groups_for(self, principal: PrincipalRef) -> list[GroupRef]: ...
    def resolve_group(self, ref: GroupRef) -> Group | None: ...
    def members_of(self, ref: GroupRef) -> Iterator[PrincipalRef]: ...   # optional
    def delta(self, since: str | None) -> DeltaPage: ...                 # optional
    capabilities: frozenset[DirectoryCapability]
```

`DirectoryCapability` (`LOOKUP`, `ENUMERATE`, `DELTA`, `PUSH`) follows the
pattern already proven by `CalendarCapability` in the calendar vendor factory:
callers negotiate rather than assume, and a provider that cannot enumerate is a
first-class citizen instead of a broken one.

### 2. Adapters, day one

- **`entra`** — Graph `/users/{id}/memberOf`, `/groups/{id}/members`, and delta
  queries. Handles the overage case by construction.
- **`oidc_claims`** — reads groups directly from verified token claims. Zero
  extra network calls; the right default when an IdP emits complete claims.
- **`local`** — file-backed, mirroring `webauth`'s file store. The floor: an
  air-gapped or single-operator node has a working directory with no external
  dependency, exactly as ADR-022 requires identity to work with no external
  authority present.

Deferred but shaped for: `scim` (inbound push), `ldap`, `google`, `github`.

### 3. Pull and push are both first class

Pull (we query) and push (SCIM posts to us) normalize to the same `GroupRef` and
the same projection. Whether an institution lets us query their directory or
insists on pushing to us is a deployment fact, not an architectural one.

### 4. Membership is a projection, never a source of truth

The directory remains authoritative. Axiom keeps a cached projection with a TTL
and, where supported, a delta cursor. We never mirror a whole directory: only
groups named in configuration are resolved or enumerated, which bounds both the
privacy surface and the scale.

### 5. Resolution composes at the ADR-084 identity boundary, claims-first

`resolve_actor` builds `ActorContext.roles` from verified token claims **first**,
then asks the directory provider to fill gaps — with a directory call forced when
the token signals claims overage. Resolution stays deterministic and stays at the
boundary; no lookups happen inside `decide()`, per ADR-084. Every resolved
`ActorContext` carries an **`as_of`** stamp.

Claims-first is the default because the alternative does not survive contact with
ADR-084. "Query the directory for privileged operations" is unimplementable as
stated: an operation's sensitivity is not known until `decide()` is executing,
and resolving membership there would drag a network round-trip into the decision
path — precisely what ADR-084 closed off by fixing resolution at the boundary.

**Freshness is therefore a property of the decision, not of the resolution.**

### 6. Staleness tolerance is declared by policy and enforced as a verdict

Each rule or resource type declares a **`max_actor_age`**: minutes for an
ordinary grounded query, seconds for operationally significant actions
(committing an irreversible operation, approving a gated action, changing
authorization itself). It is authored beside the group→role mapping, as external config.

When a decision's tolerance is tighter than the actor's `as_of`, `decide()`
returns a **re-resolution challenge** rather than performing a lookup. This is
the `STEP_UP_REQUIRED` + `Challenge` shape ADR-084 already introduced so that
step-up *originates from the decision*; re-resolution is the same mechanism with
a second trigger. The boundary then re-resolves directory-first and the request
retries. No new machinery, no lookups in `decide()`, and freshness available
exactly where it is worth paying for.

### 7. Delegated authority draws freshness from the substrate, not from claims

ADR-086's guarantee — that revoking a human starves every capability derived from
them — does **not** depend on this ADR's caching behaviour. Effective authority is
the intersection with the delegator's *live* FGA permissions, and ADR-083 chose
OpenFGA specifically because its check cache is off by default: a revoked tuple
is visible on the very next `Check`.

The staleness discussed here therefore affects only a principal's **own direct
roles**, never delegated agent authority. The practical consequence is that the
**reconciler's cadence is the real freshness dial** — delta-driven sync, and
directory change notifications where the tenant permits them, in preference to
polling.

### 8. Group→role mapping is external, hot-editable config

Which AD group means `operator` is a deployment fact. It is authored as external
config in the manner of ADR-094's tier rules — first match wins, conservative
default — so re-mapping a group is a config edit, never a code change or deploy.

### 9. Tuple sync is a reconcile, not an append

A reconciler projects membership into OpenFGA tuples
(`group:<id>#member@user:<subject>`), computing an idempotent diff. **Removals
are the point.** An append-only sync would satisfy every test that adds a person
and silently break ADR-086's central guarantee, so the reconciler is specified as
a diff from the outset and its removal path is a required test, not an
afterthought.

### 10. Revocations propagate ahead of grants

Grants and revocations are not symmetric risks. A principal who gains access a
minute late is inconvenienced; a principal who *retains* access a minute too long
is an incident. Treating both with one cache policy prices the cheap case and the
expensive case identically.

The reconciler already computes removals as a diff (decision 9). It additionally
publishes a compact **recently-revoked set** — bounded, local, consulted on every
resolution at the boundary with no directory round-trip. Additions may arrive
lazily on the normal projection cadence; removals take effect on the next
resolution.

This is what makes claims-first defensible rather than merely convenient: the
dangerous half of the staleness window closes at near-zero cost, without a
per-request directory call.

### 11. Identity is keyed on the immutable subject

Principals key on the IdP's immutable subject identifier (`oid` for Entra),
never email or UPN. Display fields are cached for humans; they are never
identifiers. Handles follow ADR-020 grammar.

### 12. Failure degrades toward less authority, never more

Mirroring ADR-083's stance that a substrate outage must not brick
authorization, while inverting its permissiveness:

- Within TTL, the cached projection is used.
- Past TTL with the directory unreachable, roles degrade to **claims-only** —
  never to "last known good, indefinitely," and never to elevated defaults.
- A directory failure can only ever *remove* roles from an `ActorContext`, never
  add them.
- Staleness is surfaced on the `ActorContext` (an `as_of` stamp) so GUARD and
  audit can see that a decision rode a degraded projection.

The deterministic capability floor still sits underneath all of it, fail-closed.

## Consequences

**What this unlocks.** Onboarding becomes group membership: add a person to an
institutional group and their access follows, with offboarding automatic when
they leave. ADR-094's identity→tier grant gets its input. OpenFGA stops being an
empty graph, so resource-scoped and per-artifact relations become expressible.
ADR-086's revocation guarantee becomes true in practice rather than only in
design.

**What it costs.** A projection introduces sync lag between an institutional
change and an Axiom decision; the delta cursor and TTL bound it, and the `as_of`
stamp makes it visible rather than silent. Enumerating group members touches
personal data, which is why enumeration is scoped to configured groups and never
a directory crawl. And a new capability is a new surface to keep fail-closed —
the degradation rules above are load-bearing, not defensive garnish.

**A tenant-capability dependency.** Decision 7 makes reconciler cadence the real
freshness dial, which presumes the directory can push change notifications. Where
a tenant permits only polling, the window widens and the revoked set (decision 10)
becomes load-bearing rather than a refinement — and `max_actor_age` should be set
aggressively on operationally significant actions. Whether notifications are
available is a deployment question to settle per institution, not a code change.

**What it does not do.** This ADR does not model any domain's types and
relations, does not decide the FGA authorization model, and does not change
GUARD. It supplies facts; the decisions above it are unchanged.

### Alternatives considered

- **Claims-only.** Rejected on overage, staleness, non-IdP sources, and
  federation — see Context.
- **SCIM-only.** Rejected: it requires the institution to push, which many will
  not do, and it strands every source that has no SCIM implementation.
- **A script writing OpenFGA tuples directly.** Rejected: no seam, no provider
  plurality, no audit path, and it would encode one directory's shape into the
  graph forever — the precise failure ADR-022 warns against.
- **Mirroring the full directory.** Rejected on privacy surface and scale, for
  no gain over scoped resolution.
- **Directory-first resolution.** Rejected: it puts a network round-trip in front
  of every authenticated request and makes the directory a hard availability
  dependency for the platform, buying freshness that decisions 6, 7 and 10
  deliver more cheaply and more selectively.
- **Directory-first "only for privileged operations."** Rejected as
  unimplementable under ADR-084 — sensitivity is not known until `decide()` is
  running, and resolving there reintroduces exactly the lookup ADR-084 removed.
  Decision 6 achieves the same intent through the verdict instead.

## Implementation status (2026-08-26)

| Phase | What | Where |
|---|---|---|
| 1 | The seam: `DirectoryProvider` protocol + capabilities, `entra` / `oidc_claims` / `local` adapters, `GroupRoleMap`, claims-first `MembershipResolver` with the degradation rules (decisions 1–7) | `extensions/builtins/directory/{protocol,providers,mapping,resolution}.py` — PR #690 |
| 2 | Reconcile-not-append `MembershipReconciler` on a `TupleStore` port, `StaleProjectionError`, the recently-revoked fast path (decisions 9–10) | `reconcile.py`, `revoked.py` — PR #698 |
| 3 | Membership → `SubjectContext` (groups as contextual tuples; a stale projection contributes no grants) | `binding.py` — PR #692 |
| 4 | The cadenced projection: `axi directory sync|status`, the `[agent]` heartbeat (900 s), `JsonFileRevokedSet` (the fast path survives a restart), `JsonFileTupleStore` for nodes without a server, `AXIOM_DIRECTORY_*` configuration (decision 7 made operational) | `sync.py`, `config.py`, `stores.py`, `skills.py`, `cli.py` — PR #705 |
| edge | The seam runs on every HTTP request: `resolve_actor` + `subject_from_membership` in the `http` authz hook, so `decide()` receives roles, tenant and assurance (decision 6 in practice) | `extensions/builtins/http/actor.py` — PR #702 |
| store | The OpenFGA client that satisfies both `FgaCheckClient` and `TupleStore` — the graph this ADR feeds is now writable | `extensions/builtins/authz/openfga_http.py` — PR #704 |

Not yet: Entra `members_of` enumeration needs the `GroupMember.Read.All`
application permission on the registration (a tenant decision, not code);
`DELTA` is implemented on the adapter but the sync does not consume it yet
(full enumeration per group per tick is adequate at current group sizes).
