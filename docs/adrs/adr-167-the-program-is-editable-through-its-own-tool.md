# ADR-167: The program is editable through its own tool, with no hard dependency on any tracker or harness

**Status:** Accepted (2026-10-06)
**Related:** [ADR-161](adr-161-program-tracking-is-a-composed-clerk.md) (the composed CLERK), [ADR-165](adr-165-program-self-update-and-per-consumer-change-detection.md) (the self-update core + change log the mutations write into), [ADR-166](adr-166-program-membership-is-a-principal-not-a-tracker-seat.md) (membership is a principal + the proxy-assignee rule), [ADR-103](adr-103-directory-providers-group-and-membership-resolution.md) (the directory seam membership resolves through), [ADR-114](adr-114-mcp-authority-enforcement.md) (the transport identity gate), [ADR-020](adr-020-federation-identity-and-relationships.md) (`@name:context`), [ADR-056](adr-056-skill-as-function.md), `docs/prds/prd-program.md` (R2, R6, R14), `docs/specs/spec-program.md`

## Context

Phases 1–4 made the program data file **readable and self-updating** from any
harness: reads project to MCP/HTTP, and `sync` reconciles the file against
pluggable read-only feeders (ADR-165) — some of which are live GitLab/GitHub
watchers (ADR-162). But the *editable* surface was still "a human hand-edits
`data.json`, or a feeder overlays tracker state." prd-program R2 says the file
is edited by an agent and a human alike, and R6 defines who may write; neither
had a tool.

Two failure modes shaped the decision:

- **A tracker-shaped editor.** It is tempting to make editing the program mean
  editing the tracker (open an issue, assign a seat) and let `sync` pull it
  back. That makes GitLab/GitHub/the issue tracker a *precondition* for having
  a program at all — and ADR-166 already established that a member need not hold
  a tracker account, that a student/partner/agent principal is a first-class
  owner, and that no one is invisible for lacking a seat. An editor that
  requires a tracker reintroduces exactly the gap ADR-166 closed. It also makes
  a program unusable on a laptop, air-gapped, or before any connector is wired.
- **A parallel people registry.** It is equally tempting to let the program
  grow its own identity store — its own notion of a user, its own account
  linking, its own membership records. Axiom already has the principal
  primitive (`@name:context`, ADR-020) and the directory seam (ADR-103) that
  resolves people/groups/membership through pluggable providers. A second
  registry is a second thing to keep in step and a second place for the
  wrong-owner class of bug (the ADR-041/042 lineage) to live.

The invitation/join half has the same two temptations: invent a new token
mechanism, or require a harness to carry it. Axiom already ships a
substrate invitation primitive — the self-service keys flow
(`axiom.webauth.invitations`): a single-use, expiring, scrypt-hashed-at-rest
code whose scope can only shrink on redemption, built so a secret reaches one
machine and is minted there.

## Decision

**The program data file is the authoritative editable source of truth, edited
through the program's own tool, with zero hard dependency on GitHub, GitLab,
Claude Code, or any harness.** External trackers are optional feeders (read);
a harness is just one MCP client; neither is required to add a person, define a
lane, schedule an item, or invite a colleague.

1. **A mutation surface of skills (ADR-056), CLI-only.** `program
   person|lane|item` add/edit/remove/reassign, `program invite` / `program
   redeem`, and the read `program ownership`. Each mutation loads the file
   strictly, mutates a lossless deep copy, re-validates on save, appends a
   change-log entry, and advances the snapshot — all under the snapshot
   exclusive lock `sync` takes, so a mutation never races `sync` and `sync`
   never re-logs a mutation. The change-kind vocabulary (ADR-165) is extended
   deliberately with `person_added/removed/edited/reassigned`,
   `lane_edited`, `lane_owner_changed`, `item_edited`, and `invited/redeemed`,
   and each entry records **`by`** — the acting principal.

2. **A member is a scoped membership over the principal primitive, resolved
   via the directory seam.** The program keeps no parallel registry. A member
   is a principal (`@name:context`, the one grammar) layered with a lane, a
   role, and an optional `accounts` map (ADR-166). When a directory provider is
   configured (ADR-103), a supplied handle is resolved through
   `MembershipResolver` for advisory org roles; when none is, the handle is
   accepted as given. Resolution never blocks membership and never reaches the
   network hard — it degrades, matching ADR-166's rule that a member is never
   gated on an account and the seam's own degrade-don't-raise contract.

3. **The invitation flow reuses the gate invitation primitive.** `program
   invite` wraps `mint_invitation`; `program redeem` wraps `redeem_invitation`.
   A program invitation is a gate invitation scoped to "join this program at
   lane L, role R": the program facts ride as extra fields on the invitation
   record, which the primitive carries losslessly, and redeeming records the
   membership (honoring the missing-account onboarding finding). We inherit the
   lifecycle — single-use, expiry, scrypt-at-rest, shrink-only scope — rather
   than reimplementing it. The gate-minted webauth key is not surfaced; the
   program's concern is the membership.

4. **Mutations are identity-gated in-body, and are not anonymous MCP tools.**
   The data-driven decision — the caller must be the deputy (`program.deputy`)
   or a declared maintainer (`program.maintainers`), with an open-posture
   operator allowance for solo/dev nodes — lives in the skill body, because
   ADR-114's transport gate cannot read the deputy out of the data file.
   Structurally every mutation declares `surfaces=("cli",)`, the same floor
   `sync`/`render` use, so no mutation is reachable as an anonymous MCP tool.
   The reads (`status`, `validate`, `changes`, `ownership`) stay read-only MCP
   tools. Opting a mutation onto MCP is a deliberate choice — add `"mcp"` and
   declare `allowed_principals` (ADR-114 §1) — never the default.

5. **Ownership over time is reconstructable.** `data.json` carries the current
   owner; the change log carries the history. An owner change (item
   `owner_changed`, lane `lane_owner_changed`) is a first-class immutable
   transition recording old principal, new principal, timestamp, and who acted.
   `program ownership` reports the current owner plus the timeline from the log,
   so a principal who held ownership in the past is reported accurately even
   after they are removed from the roster.

## Consequences

- A program is fully usable with nothing else present: a laptop, an air-gapped
  node, or a node before any connector is wired can add people, lanes, items,
  and run the full invite→redeem join flow. This is proven by a lifecycle test
  that runs the whole surface with no GitLab/GitHub/Claude Code and no directory
  provider configured and asserts the change log records every step.
- The membership half inherits every improvement to the principal primitive and
  the directory seam for free, and contributes no second identity store to keep
  in step.
- The invitation half inherits the gate primitive's security properties (and
  any future hardening) without a parallel token scheme. If the gate primitive
  is ever unavailable, the fallback is a minimal local token+redeem in the
  extension that should migrate back onto the primitive — but the reuse is live
  and preferred.
- The change-kind vocabulary grew by nine kinds. That is a deliberate, documented
  edit (ADR-165); the vocabulary stays closed so a consumer can still filter and
  a renderer can still map a kind to chrome without parsing prose.
- Mutations are deputy/maintainer-gated and CLI-only by default. A deployment
  that wants an agent to edit the program over MCP must opt a specific verb in
  and bind its principals — the surface never widens silently. On an
  open-posture solo node the gate is permissive (whoever holds the shell is the
  operator), which is the platform's own open-mode semantics; the gate bites the
  moment the node raises `AXIOM_IDENTITY_POSTURE`.
- The proxy-assignee rule (ADR-166) is now applied at edit time: reassigning an
  item to an owner with no account on the tracker system records
  `item.assignment` naming the real owner and the computed proxy, and never
  posts to any tracker — consistent with the feeders, which compute the same
  thing on ingest.
