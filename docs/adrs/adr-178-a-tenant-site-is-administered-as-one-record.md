# ADR-178: A tenant site is administered as one record, with one checklist

**Status:** Accepted (2026-10-08)
**Date:** 2026-10-07
**Related:** ADR-151 (roles separate governing from reading), ADR-153 (a role is granted at a site), ADR-157 (serving budgets), ADR-164 (a node declares its functions), ADR-177 (an ingest edge is pushed to and pulled from)


**2026-10-08 (ADR-180):** a site record also carries the producing node's mode (contributor or local-first), its record of truth and its sharing policy, and the readiness checklist includes the forwarder's current transport and the last reconcile result.

## Context

Onboarding a tenant site (another organisation whose sensors send data to a host
node) is today eight or more separate operations, run by hand on two or three
machines:

1. register a push connector on the host's data platform (`axi data register`);
2. register the same connector on the ingest edge (`sources.txt` in the edge's
   config folder);
3. issue the sender's key and copy its hash to the edge's keys file;
4. issue the host's pull key for the edge;
5. invite the tenant's people (`axi gate invite`) and grant their role
   (`axi gate role`, a site roles file);
6. create a staging site with its own key for trial sends;
7. register a document-share connector (for example a Box folder) for the
   tenant's deposits;
8. confirm the conform step can read the tenant's schema (its channel map is
   loaded).

Each step works. What fails is the set: nothing records which steps a site has
had, and nothing checks the whole path end to end. In one evening of rehearsing
a tenant install, every one of these failures was found by a person, late:

| Failure | Where it hid |
|---|---|
| The edge registered connectors under names the senders don't use, so every push would have been refused | step 2 vs the sender's own config |
| The edge didn't answer the per-channel ingest summary, so the sender held every run rather than risk a duplicate | edge capabilities vs the sender's safety check |
| A key reference that resolves on macOS and Windows didn't resolve on Linux, though storing the key had worked | sender host, after install |
| The conform registered only built-in schemas; the tenant's maps were on disk and never read | host, after deploy |
| Files from the tenant's document share landed in bronze but never became rows | host, between pull and conform |
| The share's desktop sync client doesn't exist for the tenant's OS | tenant host, at install |

None of these is a missing feature. Each is a step that was done, or assumed,
without anything confirming it held.

## Decision

### D1. One site record

A site is administered through one record in a **site registry** held by the
host node: name, kind (`host` or `tenant`), the roles that own it (never
personal names), its data paths (`edge`, `share`, `direct`), its staging twin,
the schemas it sends, the keys issued to it by id, and a status (`pending`,
`active`, `suspended`, `removed`). The record is what the admin verbs read and
write; the connectors, keys, roles and folders are derived from it and can be
re-derived from it. Removing a site keeps its record (`removed`) and its data.

The registry extends what exists rather than replacing it: connectors stay in
the data platform's connector store, keys in the gate's keys file, roles in the
site roles file. The record holds references to them.

### D2. Verbs

`axi sites add | list | show | suspend | resume | remove | rotate-key | checklist`.

- **`add <site> --path edge|share|direct [--profile <name>]`** performs every
  step in the context list that its path needs, in order, **idempotently**: run
  it twice and the second run changes nothing and says so. A step that can't be
  done from this node (the edge lives elsewhere) produces the exact artifact to
  apply there (the `sources.txt` line, the keys file), and the checklist shows
  that step as `waiting` until it is confirmed.
- **`suspend`** revokes the site's sending keys and its people's sessions
  without deleting anything; `resume` re-issues by invitation.
- **`rotate-key`** issues a replacement, delivers it by invitation, and revokes
  the old key only once the new one has been used, or after a stated window.
- **`remove`** suspends, unregisters connectors and keeps the data and the
  record.

Every mutating verb writes the action audit (`axiom.infra.audit_trail`) with
who, what and the resulting record version.

### D3. Keys never pass through an operator's screen

A sending key is issued on the host, its **hash** goes to the edge's keys file,
and its **plaintext** goes to the tenant only as a single-use invitation code
they redeem into their own vault. The host's pull key goes straight into the
host's vault. No verb prints a usable key; `show` lists key ids, scopes, last
use and expiry.

### D4. The checklist is the definition of "ready"

`axi sites checklist <site>` runs every check below and prints each as `ok`,
`waiting`, `failing` (with the fix) or `not applicable`. It reads only; it is
safe for an agent to run at any time.

| Check | How it is verified |
|---|---|
| Record complete | required fields present; data path declared |
| Host connector registered under the sender's source name | connector exists; its name equals the `source` the site was told to send as |
| Edge accepts the source | the edge reports the source in its accepted list (or the artifact is pending) |
| Sending key issued and redeemed | key exists, bound to the site, scope push-only; invitation redeemed; first use seen |
| Pull key works | a no-op pull from the edge with the host's key returns 200 |
| Edge answers the ingest summary | the summary for the site's window returns per-channel counts, not "unanswerable" |
| Sender can resolve its key on its OS | the sender's last heartbeat or support bundle reports the key resolved (a failed resolve is reported by the sender, never inferred) |
| Schema readable by the conform | every schema the site sends has a normalizer in the conform's registry, built the way the conform builds it |
| Share deposits become rows | for each recent file pulled from the share, rows exist in bronze for it |
| Share reachable from the sender's OS | the declared share sync method is supported on the sender's OS |
| Rows reach silver | the conform has produced rows for the site in the last pass, or explains why not |
| Staging separated | the staging site exists, has its own key, and has written nothing into the production site |
| People can sign in and see only their site | at least one person holds a site role; a scoped read as that role returns only this site |
| Heartbeat recent | the sending node's heartbeat is within the liveness window, or the node is declared not yet installed |

Each row of the context table is a test case: a site built with that defect
must show the matching check as `failing`. The checklist ships with those
tests.

### D5. One function, three surfaces

The verbs are skill functions (ADR-056) exposed on the CLI, as MCP tools and as
agent tools. `list`, `show` and `checklist` are read-only and available to
anyone holding the site-admin read scope; mutating verbs require the site-admin
role and are never auto-approved for an agent. A web page in the shared UI kit's
Settings area ("Sites") renders the same record and checklist and calls the
same skills; it adds no logic of its own.

### D6. Profiles come from the consumer

`--profile <name>` applies defaults a consumer layer registers through an entry
point: schema templates, a channel-map scaffold, the sender's install bundle
and the guide to send. Axiom ships no profile; it defines the shape and applies
what is registered.

### D7. Layering

Applying the three questions in AGENTS.md: the registry, verbs, key flow and
checklist mechanics survive removing every domain noun, are wrong only in ways
any operator can see, and read no consumer artifact, so they belong here. Which
schemas a kind of site sends, its channel-map scaffold and its install guide
fail all three, so they live in the consumer's profile.

## What exists and what is missing

| Piece | State |
|---|---|
| Site roles and grants (ADR-151/153), site roles file, `axi gate role` | exists |
| Invitations, redeem into the vault | exists (`gate.invite`, `gate.redeem`) |
| Connector registration and enrollment | exists (`data.register`, `data.enroll`) |
| Ingest edge with a live-reloaded config folder (sources and hashed keys) | exists on a branch (`feat/ingest-edge-docker`); to land first |
| Topology hops with `planned` states (ADR-164) | exists (`axiom.infra.topology`) |
| Node identity, binding, `whoami` | exists |
| **Site registry (one record per site)** | **missing**: today a site is implied by the connectors, keys and roles that mention it |
| **`axi sites` verbs and skills** | **missing** |
| **Checklist and its defect tests** | **missing** |
| **Edge-side acceptance report** (which sources it accepts) | **missing**: the edge logs it; no endpoint reports it to its owner |
| **Sender-side key-resolution report** in heartbeat/support bundle | **missing** |
| **Settings "Sites" page** | **missing** |

## Consequences

- Adding a tenant becomes one command and a checklist that is either green or
  says what to fix. The checks that caught nobody tonight run every time.
- The registry is a new source of truth that can drift from the stores it
  references. Mitigation: `checklist` compares record and stores and reports any
  drift; the stores remain authoritative for enforcement.
- The edge and the sender must report two facts they don't report today (D4
  rows on acceptance and key resolution). Until they do, those checks show
  `waiting`, never `ok`.
- Admin verbs are a new privileged surface. They sit behind the site-admin role,
  are audited, and never print a usable key.

## Alternatives considered

- **A runbook only.** Tonight had a runbook-shaped process and still missed six
  defects; a document can't verify.
- **One script per consumer.** Duplicates key handling and audit per consumer,
  and the checks would drift between them.
- **Infer a site from existing stores, no record.** Works for `list`, but gives
  `add` nothing to be idempotent against and `remove` nothing to keep.
