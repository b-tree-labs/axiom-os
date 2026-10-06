# Product Requirements (One-Page)

**Product / Feature:** Program tracking (the `program` extension and the CLERK agent)

**Owner:** Platform (B-Tree Labs / UT)   •   **Status:** Draft   •   **Last updated:** 2026-10-05 (R11 drift resilience added)

---

## 1) Elevator Pitch
Keep a program's tracker, status site, and wiki current as a side effect of
people's regular work, from any harness or none, with so few parts that one
person who did not build it could run it.

## 2) Problem / Opportunity
- Program status today is assembled by hand: evidence gathered from repos and
  the journal, progress comments drafted and posted, priorities proposed,
  a site rendered, assignees reconciled. The first full cycle was run manually
  on 2026-10-05 and took a working day.
- Status reporting decays when it is a chore; evidence does not. The work
  people already land (merge requests linked to issues, signed records,
  registry events) is the report, unassembled.
- Nothing existing composes the pieces: the connections library, the event
  bus, the scheduler, the delegation spine, and the journal all exist and are
  not joined for this purpose.

## 3) Goals & Success Metrics
- Primary goal: nobody reports status, and the program's surfaces are never
  more than an hour stale.
- Success metrics:
  - Weekly cycle time — from a working day by hand to under ten minutes of
    human review (the deputy's approve-and-confirm pass).
  - Staleness — publication endpoints refresh within minutes of an event,
    healed within the hour worst case.
  - Coverage — every active contributor's landed work appears in the record
    with a source reference; zero invented claims (every drafted sentence
    cites collected evidence).
  - Attachment — assignees, deciders, and watchers on tracker items match the
    ownership map continuously; principals without tracker accounts surface
    as onboarding tasks, never as silent gaps.

## 4) Key Users / Personas
- The program deputy: runs the review pass, confirms priorities, owns consent.
- Lane owners in any harness (or none): ask status, post their own progress,
  commit their own dates; or do nothing and remain covered by evidence.
- Leadership: reads the published status site and a delivered digest.
- Partner programs and principals without tracker accounts: read a scoped,
  rendered projection through a node's program face.
- Agents: lane owners may be agent principals; their journaled work is
  evidence like anyone's.

## 5) Requirements
- R1 Seven skills as commons, invocable by any principal through the composed
  MCP and the CLI: `status` (the read tool: one parameterized query returning
  items with owner, dates, proposed-or-committed state, percent, evidence
  references, and links), `collect`, `draft` (every claim cites evidence),
  `post` (consent-tiered: propose, then ask, then back off), `priorities`,
  `render`, `attach` (keep the right people attached; see Goals).
- R2 One data file per program (`data.json`): principals, lanes, items with
  host-qualified tracker bindings, dates, status, percent, evidence refs.
  The site renders from it; the agent updates it; a human can edit it.
- R3 One thin coordinator agent (CLERK, per ADR-161) on the scheduler,
  delegating specialist steps over the spine; all its writes are requester
  attributed and journaled.
- R4 Capture rides the watcher primitive (ADR-162) onto the event bus:
  webhooks where they reach, cursor delta scans where they do not, an hourly
  heal tick; branches are first-class evidence (landed vs in-flight reported
  distinctly); mirrors never double-count.
- R5 Endpoint classes with one home each: living status on the site, progress
  records on tracker items, produced permanents on the wiki; the extension
  refuses to write one class to two homes.
- R6 Access: reads for resolved program principals through the gate; write
  verbs self-scoped; the deputy may act for others, reviewed; meetings and
  chat rooms are read-only surfaces that can at most create proposals.
- R7 Federation: per-audience projections rendered from opt-in share tags,
  delivered as signed projections; no inbound writes; a node serves its slice
  under its own gate.
- R8 Composition: programs roll up clerk-to-clerk (a child's published summary
  is a parent's evidence source); authority never inherits downward; scale is
  configuration, not code.
- R9 Connector readiness is a precondition: a source is used only after the
  verification ladder (reach, auth, per-project read, scratch write where
  writes are declared, webhook delivery onto the bus).
- R10 Trackers, publishers, wikis, and chat/meeting sources are connectors
  chosen by the deployment; the extension names no vendor and no domain.
- R11 Drift and mirror resilience. The system is reconciled, not merely synced:
  each cycle compares the data file against the tracker's actual state (item
  existence, assignees, task-list and roll-up membership) and repairs or
  reports divergence. The deployment declares which repositories should mirror
  one another; the watcher confirms both sides agree (same commits present, the
  mirror job ran recently) and flags a repository tracked on one host with no
  mirror where one is expected as a gap, never silent half-tracking. Orphans
  (an item with no lane, an owner with no account, a repository with activity
  but no tracked item) are standing drift signals, not one-off audits. A sync
  state a connector cannot confirm is reported as unverified, never assumed
  synced. The `status` skill gains a `drift` scope so any surface can answer
  what is out of sync. Reconciliation is idempotent: re-running a cycle
  converges to the same state, and no operation assumes it is the only writer.

## 5a) Drift and mirror resilience (R11 in detail)

The hard scenario: activity happens in one repository and its mirror does not
receive it, so the program is tracked on one host and blind on the other. The
design makes this unlikely and, when it happens, loud.

- **Reconcile every cycle.** The data file is the convergence target. A cycle
  reads the tracker's real state and brings the file and the surfaces back into
  agreement, so a manual edit, a missed webhook, or a second writer cannot
  leave a surface permanently wrong — the next cycle heals it.
- **Mirror pairs are declared and checked.** A deployment lists the host pairs
  that should mirror. The watcher verifies the pair agrees and that the mirror
  job is recent; a divergent or stale pair is a flagged gap. Capture keys on
  commit identity, so a commit arriving through both origin and mirror collapses
  to one event rather than double-counting.
- **Absence has kinds.** Confirmed-synced, confirmed-gap, and unverified are
  three distinct states; unverified is reported as such, never rounded up to
  synced. This mirrors the uncertainty posture elsewhere in the platform.
- **Orphans surface continuously.** The three drift shapes found by hand during
  bring-up (item without a lane, owner without an account, active repository
  without a tracked item) become standing signals on the `drift` scope.

Reconciliation is a build-order phase after capture exists: it needs the
evidence feed before it can compare against it.

## 6) Out of Scope (for now)
- The tech spec (lands with the implementation, same PR, per doc standards).
- Live meeting attendance (its own consent decision; passive transcript
  capture is the default).
- Auto-posting without review (graduates per item class as trust is earned).

## 7) Links
- ADR-161 (the composed CLERK), ADR-162 (the watcher primitive), ADR-060
  (event routing), ADR-056 (CLI verbs over skill functions), the oversight
  surfaces work (ADR-156, in flight).
- Consumer-side planning narrative and program-specific configuration live in
  the consumer repo's docs, not here.
