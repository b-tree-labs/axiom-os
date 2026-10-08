# Product Requirements (One-Page)

**Product / Feature:** Program tracking (the `program` extension and the CLERK agent)

**Owner:** Platform (B-Tree Labs / UT)   •   **Status:** Draft   •   **Last updated:** 2026-10-06 (R23–R25 added: emergent work and divergence, lifecycle for people and products, and absence-as-follow-up — ADR-176, continuing the R15–R22 lineage of the adaptivity posture, the product spine and its North Star apex, the roadmap instrument, deterministic estimation, conversational shaping, attachments/deep-links, and the delivery-is-a-loop ethos — ADR-171/172/173)

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

The defining posture is **adaptivity with near-zero external dependency**
(R15, ADR-171): the data file is the authoritative source of truth, every verb
works with nothing else present, and every external system is an optional,
additive, gracefully-degrading connector. Every requirement below is read under
it.

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
  attributed and journaled. **The coordinator self-updates continuously:** on
  its heartbeat it runs `sync` (below), so the program's recorded state is
  never more than one cycle stale and a silent stop is visible because the
  clerk's run is itself journaled. Declaring the agent does not force
  autonomy on — firing stays an operator decision (the master autonomy
  toggle and per-agent consent).
- R4 Capture rides the watcher primitive (ADR-162) onto the event bus:
  webhooks where they reach, cursor delta scans where they do not, an hourly
  heal tick; branches are first-class evidence (landed vs in-flight reported
  distinctly); mirrors never double-count. **The `sync` reconcile and the
  change log are the detection core those feeders write into:** phase 3 shipped
  `sync` with a pluggable read-only source seam (`FileSource`/`NullSource`);
  **phase 4 ships the live `GitLabSource` and `GitHubSource`** as instances of
  one shared watcher primitive (`axiom.infra.watcher` — cursor delta,
  debounce, commit-identity dedup), selected by `sync`'s `source_kind` (file |
  gitlab | github | all) and configured per deployment via `program.feeders`.
  Branches are first-class (landed vs in-flight); mirrors collapse on commit
  SHA; writing back to a tracker is a later phase (phase 4 ingests, attributes,
  and detects). Webhooks and the hourly heal tick remain a later option.
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
  writes are declared, webhook delivery onto the bus). **Phase 4: a live
  feeder climbs reach → auth → per-project-read before `sync` uses it; one
  that stops short is reported `unverified` and skipped loudly — never read as
  "no changes". Scratch-write and webhook-delivery rungs arrive with the
  posting and webhook phases.**
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
  **The idempotency is a content hash per item and per field held in a
  snapshot beside the data file:** `sync` diffs the current source against
  the last snapshot and appends one change-log entry per difference, so a
  change seen twice is not logged twice. `data.json` stays the current
  state; the append-only change log is the history beside it.
  **Crosslink health (phase 5): the tracker continuously proves its links
  resolve.** Each feeder cycle checks, read-only, that every tracker-bound
  item's `issue` binding and every declared endpoint still resolves; a
  confirmed-dead link is a `dead_link` drift finding that flows through the
  change log (`dead_link_opened`/`dead_link_cleared`) and the `changes` read,
  recording the correct backlink target under `proposed_fix` for the later
  posting phase — detection only, never a write to the tracker. A checker that
  cannot reach a link reports it `unverified`, never `healthy` (unverified ≠
  synced). The program declares its stable **canonical endpoints**
  (`program.endpoints`), so a backlink targets a durable endpoint rather than a
  volatile artifact URL; `status`/`validate` surface them. (There is no
  node-public-URL primitive today, so the absolute canonical URL stays
  deployment-config-supplied.)
- R12 Per-consumer change detection. Beyond "what is the state" and "what is
  out of sync", a principal can ask **"what changed since the last time the
  program reported to ME"**. Each principal has a watermark — the change-log
  position it was last reported up to — and the `changes` read returns the
  entries after it, in the same owner/dates/status/percent/links shape as
  `status`. Reporting those deltas is a read; *advancing* the watermark is a
  write, and the two are separated so the capability projects as a read-only
  MCP/HTTP tool: advancing is the default only on the operator's own CLI, is
  peek (non-advancing) by default on every served surface, and otherwise
  requires an explicit advance. The watermark write is the caller's own local
  reading position — never program state, never anything another consumer
  observes.
- R13 Identity, attribution, and the proxy assignee (ADR-166). **Program
  membership is a principal** (`people[]`, `@name:context`), not a tracker
  seat. A person may carry an optional `accounts` map (`{gitlab, github, …}`,
  each a username or an explicit `null`). Feeders **attribute** external
  activity to the principal behind the account (a merge request by an account
  username → the principal whose `accounts[system]` matches); a schedule
  item's real owner stays the principal and stays authoritative in the data
  file. For a tracker-bound item whose real owner has **no** account on the
  system, the feeder computes the *intended* **proxy assignee** — the lane
  lead (`lanes[].lead`) when the lead has an account, else the deputy
  (`program.deputy`), else none — names the real owner regardless, and records
  the result for a later posting phase (nothing is pushed to the tracker in
  this phase). The owner's missing account surfaces as an onboarding finding
  (`account_missing`, tracked `pending`) on the drift/`changes` surface, never
  as a silent gap. People with no tracker account read their status and deltas
  through the node's own surfaces; feeders never gate membership on an account.
- R14 The program is editable through its own tool, independent of any external
  tracker or harness (ADR-167). The data file is the **authoritative editable
  source of truth**: `program person|lane|item` add/edit/remove/reassign its
  members, lanes and schedule items, and `program invite` / `program redeem`
  are a join flow, **with zero hard dependency on GitLab, GitHub, Claude Code,
  or any harness** — external trackers are optional feeders (read), a harness
  is one MCP client, and neither is a precondition for editing. A program
  member is a scoped **membership** over the principal primitive (`@name:context`
  + lane + role + optional accounts), resolved through the directory seam
  (ADR-103) when a provider is configured and accepted as given otherwise —
  **not a parallel people registry**. The invitation flow **reuses the gate
  invitation primitive** (self-service keys: single-use, expiring, scrypt-
  hashed, shrink-only scope); redeeming records the membership and honors the
  missing-account onboarding finding. Every mutation writes the data file
  through the locked state helpers, appends one change-log entry recording who
  acted (so an owner change is an immutable transition and ownership-over-time
  is reportable — current from the data file, history from the log, a past
  owner named accurately even after they leave the roster), and is identity-
  gated (the deputy may act for others — R6 — or a declared maintainer; an
  open-posture solo/dev node lets the operator edit). Reads stay read-only MCP
  tools; mutations are CLI-only and never anonymous MCP tools — opting a
  mutation onto MCP is a deliberate, principal-bound choice, never the default.
  **Account-aware reads (phase 5): the non-tracker viewer sees content, not a
  login wall.** `status` surfaces each item's captured tracker content (title,
  state, last activity) so a consumer who can see the tracker sees the item's
  substance without opening GitLab/GitHub, and marks every external link with
  an `access` hint and a `gated` flag — "edit / see more if you have access",
  not the only way in. Absent feeder content is reported absent, never invented.
- R15 Adaptivity and near-zero external dependency — the defining posture
  (ADR-171). The program tool is self-sufficient: the data file is the
  authoritative source of truth, and **every verb works with no external system
  present**. Every external system — a tracker (GitLab/GitHub), a wiki, any
  harness (including Claude Code), the directory/IdP, the vault — is an
  **optional, additive, gracefully-degrading connector**, used only where it is
  configured **and** available **and** additive; absent or unauthenticated, the
  tool reports the affected facts **unverified** and **skips loudly** (never
  rounding unverified up to synced), and never breaks. The platform **meets
  people where they already work** — it ingests a contributor's wiki or
  repository as an evidence source rather than forcing them onto a new tool — and
  a new source or a new product type is **data and configuration, not code**.
  R10, R9, and R14's zero-hard-dependency rule are instances of this one posture.
- R16 Products are the spine (ADR-172). Every schedule item is accountable to at
  least one product — a **hard rule, no exception** (an item that produces
  nothing is a drift finding; a product with no items is a wish; a product past
  its target with no evidence is a slip). A product is a first-class record
  (`products[]`): id, name, type, lifecycle, owner (a principal), optional
  target, the items that produce it (`schedule[].produces`), and evidence
  crosslinks. Products are **infinitely composable** through three relationship
  kinds — composition (`composed_of`), dependency (`depends_on`), and lineage
  (`derived_from`) — so the product space is a **graph**, and status and evidence
  roll up along it. **Seven product-type families** ship (software, physical,
  model/data, research, communication, program, composite); the type and its
  lifecycle are **deployment data, not code** (R10), and a new family or
  lifecycle is configuration. Product **evidence is a crosslink** to a system
  that already holds the proof — a release, a model-registry entry, a signed
  journal record, a research-surface page — **never a copy**; crosslink health
  (R11) watches every such link, and an absent evidence system makes the link
  unverified, never breaks the product (R15).
- R17 The accountability ladder has an apex (ADR-172): item → product → program →
  **North Star**. The North Star is a single visionary statement of the world
  when the program has succeeded — one field (`program.north_star`), surfaced at
  the top of every program face, changed rarely. It is the apex every item is
  ultimately accountable to, and the feedback loops (R22) measure distance toward
  it. The platform carries it verbatim and never generates it.
- R18 The roadmap is an instrument, not a static view, **revealed in layers**
  (incremental revelation; the simple static view is the default and everything
  else stays out of the way until reached for). Five layers: **see** (static
  views by product, person, time, or lane, printable on demand), **reshape**
  (filter / group / zoom — non-mutating), **search** (find an item, a product, or
  a recorded-but-uncommitted idea, and jump to it), **model** (non-destructive
  **scenario forks** for what-ifs — add resources, de-emphasize or drop,
  buy-vs-build, re-sequence — each rendered beside the baseline to compare), and
  **ask** (an agent drives the modeling from conversation — R20). A **mind-map
  view** renders products and items as one graph carrying all three edge kinds
  (composition / dependency / lineage) and the produced-by edges, with nodes that
  expand and collapse so it stays readable.
- R19 Estimation is **deterministic and self-calibrating** (ADR-173). The date
  math is **computed, never guessed**: a forecast is measured throughput (drawn
  from the change log, per person / lane / size, kept as a **distribution** rather
  than one velocity) applied to sized remaining scope, along the dependency gates,
  under the resourcing. The language model's **only** role is to **propose a
  t-shirt size** (XS–XL, from similar past items; one-tap confirm, optional — an
  unsized item takes the proposal) and, rarely, to ask one short question; **it
  never invents a date**. Resourcing is **auto-pulled** from the data; the tool
  asks at most one or two questions, and only where the data is silent (99%
  automated — a form nobody fills in is a tracker nobody trusts). An answer is a
  **range with its stated assumptions**, carrying its uncertainty per
  `axiom.uncertainty`, never a false-precise single day. Calibrated sizes **flow
  back** into the tracker's estimate/weight fields, consent-gated.
- R20 Conversational shaping. A principal shapes the program **by talking to
  it**. An agent poses a dynamic, data-grounded question bank — the fewest,
  highest-information trade-offs the data reveals (top priorities, what could slip
  without cost, where one more person changes the most, buy-vs-build, what must be
  true by a fixed date) — and turns the answers into **proposed changes** (priority
  order, emphasis up or down, de-emphasis or drop, reassignment, target moves) on
  a **scenario fork**, shows the consequence through the forecast (R19), and
  enacts only on human adoption, through the same mutation verbs (R14/ADR-167),
  logged. **Propose → approve → enact:** nothing lands unapproved, and every
  enacted change traces to what was said. It reuses scenarios, the forecast, the
  mutation verbs, the consent tiers, and the change log — no new engine.
- R21 Attachments and deep-link/share. Every node — program, lane, product, item,
  person — carries a list of **attached URLs**, auto-inferred (a release tag, a
  DOI, a registry entry, a tracker issue, a node page) and manually added (a doc,
  a thread, a dashboard, a paper), each marked auto or manual and each a crosslink
  watched by crosslink health (R11). Every aspect of the program — a product, an
  item, a lane, a person, a view, a scenario — has a **stable, shareable
  deep-link** served durably by the node's program publication endpoint
  (`program.endpoints`, R11), not a volatile artifact URL, and opening it lands
  the recipient **under the gate** (a member sees it in full, a partner a scoped
  projection per R7, an anonymous caller nothing).
- R22 Delivery is a loop, not an end — the ethos (cross-cutting, not a feature).
  A product's lifecycle **does not stop at delivered** but continues (in use →
  iterating → adopted); a product ships **minimal and viable** (v0.x) and iterates
  toward adoption. **Usage and feedback are ingested signals** on the same
  evidence path as commits and issues (R4/ADR-162), and they feed prioritization
  (R18/R20) directly. The system improves **recursively**, calibrating its
  estimates (R19) and its proposed priorities from **outcomes**, not just from
  closed work — the loop closes on the tracker itself. The loops **nest** at
  feature, product, and program scale, and the roadmap shows not only a ship date
  but the loops that follow it.
- R23 Emergent work and divergence (ADR-176). Work that was **done but never
  planned** is a first-class flow, opposite in direction to planned work (planned
  work flows intention → delivery; emergent work arrives as a delivered fact the
  plan must account for). The capture feeders (R4) already observe all work;
  landed work that maps to **no tracked item** is *emergent* and lands in an
  **inbox** — never silently absorbed into the plan, never silently dropped. This
  is orphan detection at the **work** level, one above the orphan contributor and
  orphan issue (R11). Each emergent item is **classified** on a single alignment
  axis — does it ladder up to an existing product and the North Star (R16/R17), a
  likely improvement, or not, a candidate side-trail — which the tracker
  **proposes** and never decides. A human, in the conversational-shaping flow
  (R20), **adopts** it (create a tracked item, attach to an existing **or a new**
  product — capturing serendipity), names it a **distraction** (stop or park), or
  **defers** it; the decision is recorded with its rationale in the change log, so
  the log is an audit of **why the program changed course**. A **divergence view**
  plots emergent work against the plan by alignment (advancing toward vs pulling
  away from the North Star) over time, making the discovery-versus-drift balance
  visible. Strictness comes from **visibility, not prohibition**: nothing is
  forbidden, no side-trail stays invisible, every divergence surfaces for an
  explicit decision, and the tracker may **gently flag** (never block) an actor
  accumulating divergent untracked work; serendipity is protected by making
  **adoption cheap** — one step from discovery to a tracked product. It reuses the
  feeders (R4/ADR-162), the accountability ladder (ADR-172), the mutation verbs
  (ADR-167), the shaping flow (R20), and the change log (ADR-165) — no new engine
  (ADR-171).
- R24 Lifecycle for people and products — a contributor who moves on is credited,
  not deleted; a product that is replaced records what it replaced. A person
  carries a **status** — `active` / `alumnus` / `historical` — so a contributor who
  has moved on is kept on the record with their contributions credited, their past
  ownership intact (R13/ADR-166's ownership-over-time), and their name reported
  accurately, never removed as if they had never been there. A product carries a
  **lifecycle** — `active` / `legacy` / `superseded` — and a `superseded_by`
  pointer, so a replacing product records what it replaced and the history is
  **shown, not erased**. (This is the delivery-is-a-loop lifecycle of R22 extended
  to the end of life: a product does not vanish when a successor ships; it moves to
  `legacy` or `superseded` with the link that explains why.)
- R25 Absence is a follow-up, not a judgment. A tracked actor with **no current
  milestone** renders **forward-looking** — "to be set at the next sync" — never as
  a blank cell and never as a deficiency. That absence rolls up into a **private**
  program-head **follow-up list** (the tracker generates the owner's check-in
  agenda from the actors who have nothing scheduled), so absence routes to a
  **human decision** at the next sync, never to a silent verdict rendered on a
  public surface. This is the people-and-milestones instance of the platform's
  absence posture (R11: unverified is not synced; absence has kinds): a missing
  milestone is an open question for a person to answer, not a fact about them.

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
- ADR-161 (the composed CLERK), ADR-162 (the watcher primitive), ADR-165
  (self-update + per-consumer change detection), ADR-166 (membership is a
  principal; feeders attribute to it), ADR-167 (editable through its own tool,
  no hard dependency), ADR-171 (adaptivity / near-zero external dependency —
  the defining posture), ADR-172 (products are the composable spine, up a ladder
  to a North Star), ADR-173 (estimation is deterministic; the model proposes a
  size, never a date), ADR-176 (emergent work and divergence — detected,
  classified, and decided, never silently absorbed), ADR-060 (event routing),
  ADR-056 (CLI verbs over skill functions), the oversight surfaces work (ADR-156,
  in flight).
- Readable design narrative: `../design/program-tracking.md` is the "why it is
  the way it is" companion to this PRD, the spec, and the ADRs above. It is the
  front door; this document and the records it links are the source of truth.
- Consumer-side planning narrative and program-specific configuration live in
  the consumer repo's docs, not here.
