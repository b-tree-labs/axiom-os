# PRD: Gold serving safeguards: no single call can overrun the serving tier

**Product / Feature:** Serving-tier protection for every verb that reads the gold tier, through the CLI, MCP, HTTP and agent tools

**Owner:** Ben Booth   •   **Status:** Draft   •   **Last updated:** 2026-10-05

**Related:** [ADR-157](../adrs/adr-157-the-serving-tier-bounds-what-any-one-call-can-cost.md) (the decision this PRD sequences), [ADR-114](../adrs/adr-114-mcp-authority-enforcement.md), [ADR-128](../adrs/adr-128-medallion-tier-boundaries.md), [ADR-131](../adrs/adr-131-there-is-always-a-medallion.md), [ADR-132](../adrs/adr-132-sensing-faults-detect-carry-refuse.md), [ADR-136](../adrs/adr-136-uncertainty-is-a-platform-primitive.md), [prd-access-agreements](prd-access-agreements.md)

---

## 1) Elevator pitch

The gold tier serves measured values to people and to agents. Today nothing but each verb author's care decides how much a single call can read, how much it returns, or how many a caller may make at once. Axiom should enforce those limits itself, at the one door every access path passes through, so that an ignorant or a hostile consumer can neither take the tier down nor drown their own context window with an answer too large to read. A call that asks for too much gets a smaller, honest answer, a way to page through the rest, or a precise refusal that names the cheaper question.

## 2) Problem / Opportunity

The gold tier is about to be reachable by people we do not control. Colleagues connect agents through MCP, a keyed chat API is opening to programs, and the gate now issues keys to individuals. The inventory below was taken from code on 2026-10-05, and the live settings were read from the production database the same day. Items marked MEASURED were observed on the running system; items marked CODE are shown by a file and line in the consumer layer's own inventory; items marked UNVERIFIED could not be proven.

- **The database roles that serve data are not protected.** MEASURED: every serving role except one has no role-level settings at all. The server's own defaults are `statement_timeout` 0 (none), `lock_timeout` 0, `idle_in_transaction_session_timeout` 0, `temp_file_limit` unlimited, and `max_connections` 100, with no per-role connection limit. The one exception is a read-only kiosk role that has a 30 second statement timeout and a 60 second idle-in-transaction timeout. So a single query can run for hours, spill without bound onto a volume that is 91 percent full, and 100 simultaneous calls take every connection.
- **A call can touch an unbounded amount of data.** CODE: the generic series, aggregate and compare verbs have no row limit, no required window, no statement timeout, and accept a bucket of `1 microsecond`. Each call also runs up to six additional scans over the same rows. Several consumer verbs return one entry per bucket with no cap and no truncation flag; one has a parameter that, set to zero, returns every sample in the span.
- **A call can return an unbounded amount of data to a consumer that cannot hold it.** MEASURED: a series call with no limit returned 327 KB, and a colleague's harness refused 459,096 characters. Narrowing the window to five minutes still returned 196 KB, because the data arrives at 10 samples a second. CODE: the MCP layer returns the whole result as indented JSON with no size cap and no pagination.
- **Truncation lies by omission.** MEASURED: a 24-hour request returned the oldest 5,000 points, which cover 8.3 minutes, 0.58 percent of what was asked, while the response echoed the requested day as its window. The honest fields exist, but the payload is too large for the consumer to read them.
- **Rejection costs as much as success.** MEASURED: a 30-day aggregate is refused only after running the full 20 second timeout, and nothing stops a caller repeating it. There is no per-caller rate limit, concurrency cap or quota anywhere in the consumer pack, the HTTP mount, or the front door.
- **One chat turn can fan out without bound.** CODE: a model may emit any number of tool calls in each of up to five rounds, and every one is executed. Their results go into the conversation uncapped. The front door allows a response to run for an hour.
- **Some verbs scan all history on every call, and one is reachable without authentication.** CODE: the data-freshness verb counts every row of a large table on each call and is also called by the unauthenticated readiness probe.
- **A caller can choose the database.** CODE: the generic verbs honour a caller-supplied connection string, and the consumer's telemetry tools document an `endpoint`, `token` and `dsn` override per call. Whether the MCP layer strips an undeclared argument such as `dsn` is UNVERIFIED.
- **Authorization is coarse and mostly off.** MEASURED: the enforcement flag for per-capability checks (`AXIOM_AUTHZ_ENFORCE`) is unset on the node. CODE: scopes grant a whole mount, so a token that can read the telemetry mount can name any site in the table. The export-control gate classifies a result after the query has already run and been paid for.
- **The cost of a query depends on its shape, and nothing enforces the cheap shape.** MEASURED: the same aggregate costs 0.33 ms with the full index prefix and 977 ms with one key column missing, about 3,000 times more. A latest-per-site view over about 70 million rows took 7.3 seconds. The generic verbs do not require or supply the index prefix.
- **There is no headroom for a mistake.** MEASURED: the database is 528 GB on a volume that is 91 percent full with about 90 days of runway, and a 2-day grouping over the largest raw table could not finish in 150 seconds on a live system. Compression measured 113.9 times on a probe but is not applied.

The opportunity is that all of this can be enforced in one place. The serving layer already sees every call, and the gate now supplies a verified principal for each one.

## 3) Principles (the invariants every phase must keep)

1. **No call is unbounded.** Every call has a default and a hard maximum in points and in bytes, set by the kind of caller. There is no flag that removes them for convenience.
2. **A limit never silently discards the part the caller most likely wants.** Over a ceiling, a call is reshaped across the whole window, paginated, or refused. It is never cut to its oldest part.
3. **The response says what was done, first.** Requested, returned, covered, resolution, reduced, and the cursor lead the response, so a consumer that stops reading early has already seen them.
4. **A reduced answer keeps the truth that reduction usually loses.** Minimum, maximum, mean and count per bucket, with fault codes and uncertainty carried through. A fault code is never averaged into a plausible value (ADR-132, ADR-136).
5. **The limit lives at the door, not in the verb.** A verb declares what it is. The layer decides what is allowed. A verb with no declaration is refused.
6. **The caller cannot widen their own limits or redirect the database.** The connection, the role and the ceilings come from the deployment. A request cannot carry them.
7. **The quota belongs to the verified principal.** Identity comes from the gate, never from a header a caller can set. Anonymous gets the smallest budget.
8. **Every limit leaves a trace.** Use is metered per principal, a raised ceiling is recorded, and a principal can be suspended at once.
9. **Cost is estimated before the query runs, and refusal is cheap.** A call that cannot fit its budget is refused before it touches the database.

![A caller passes the gate, which verifies the principal; the serving layer checks the verb's cost class against the caller class and estimates cost before the query runs; a call that fits proceeds through a restricted serving database role and is answered from rollups where possible; a call that is too large is reshaped, paginated, or refused with the cheaper call named; every response carries the bounded envelope stating requested, returned, covered, resolution and cursor first.](../assets/serving-safeguards-flow.png)

*(Diagram: `docs/assets/serving-safeguards-flow.png`, regenerated by `scripts/diagrams/serving_safeguards_flow.py`. Diagrams in these docs are committed PNGs with their generator in the repo; Mermaid is not used.)*


## 4) Goals & Success Metrics

- **Primary goal:** no single call, and no single caller, can degrade the gold tier for anyone else, and no answer is too large for the consumer it was made for.
- **Success metrics:**
  - 100 percent of gold verbs carry a declared cost class; a verb without one is refused at load.
  - Every serving role has a statement timeout, a lock timeout, an idle-in-transaction timeout, a temporary-file limit and a connection limit, verified by a check that fails the deploy when one is missing. Today 1 of the 6 non-system roles has any.
  - No response to an MCP agent exceeds its byte budget; a series request over a wide window returns a sample that spans the whole window, never its oldest part.
  - The abusive-consumer battery (an unbounded range, an enormous window, a parallel flood, hostile parameters, an anonymous caller) passes against every verb in the release gate, and fails a build that removes a limit.
  - A principal can be suspended in under one minute with no deploy.
  - A hostile consumer making calls at the maximum permitted rate leaves other consumers' median latency within 20 percent of idle.

## 5) Key Users / Personas

- **Operator at the UI or CLI:** asks bounded, ordinary questions; must never notice the limits except as a clear message when a request is too wide.
- **Researcher or colleague with an agent:** an MCP client with a context window; needs answers that fit it and that say plainly when they are coarse.
- **Service or pipeline:** a program with a larger, declared budget and an identity of its own.
- **Anonymous or public caller:** the smallest budget and the narrowest tier.
- **Administrator:** sets the policy table, sees usage, and can suspend a principal, without being able to read the data it protects.
- **Hostile or careless consumer:** the reason the limits exist; included in the test battery as a persona.

## 6) Scope: capabilities by phase

**Phase 0: close the doors that need no new code (configuration and small changes, first).**
1. Set role-level limits on every serving role: statement timeout, lock timeout, idle-in-transaction timeout, temporary-file limit, and a connection limit. Verify with a deploy check.
2. Stop honouring a caller-supplied connection string, endpoint or token on the verbs and tools; the deployment supplies them.
3. Cap the number of tool calls a chat turn may execute, and cap tool result size before it enters the conversation.
4. Add request-rate and connection limits at the front door and shorten its read timeout for the data routes.
5. Stop the unauthenticated readiness probe from running a full-history scan.

**Phase 1: the serving layer (the heart of ADR-157).**
1. Cost classes and a declaration on every verb; a verb without one is refused.
2. The policy table: defaults and ceilings in points and bytes by cost class and caller class, as configuration.
3. Reshape, paginate or refuse, with a typed refusal naming the cheaper call; the leading envelope fields; reduced answers carrying min, max, mean, count, faults and uncertainty.
4. Pre-flight cost estimate from the window, the native period and the channel count.
5. Bounded MCP results: a byte budget by caller class and a cursor, not one indented JSON blob.

**Phase 2: callers.**
1. Per-principal rate limit and concurrency cap from the verified principal; the smallest budget for anonymous.
2. Admission control and load shedding by priority class when the database is busy.
3. Per-principal metering into the activity journal, and the suspend switch.
4. A serving database role distinct from the roles ingest and conformance use.

**Phase 3: make the cheap answer cheap.**
1. Serve wide ranges from rollups (continuous aggregates), so raw reads stay narrow.
2. Apply compression to the largest raw table, sequenced with the storage work already planned.
3. Require or supply the index prefix in the generic verbs.

**Phase 4: keep it true.**
1. The abusive-consumer battery in the release gate, run against every verb on every release, with a negative control that proves it can fail.
2. A weekly report of the heaviest principals and verbs, and of any ceiling raised.

## 7) Non-goals

- A general query language, or letting consumers write their own SQL against the gold tier.
- Replacing database-level controls. Role limits stay as the last line; this adds the layers above them.
- Rate-limiting an operator's ordinary use. The limits are set so that careful use never meets them.
- Export-control policy. That stays with the access-agreements work; this PRD only requires that a controlled request be refused before it runs and not after.
- A per-verb tuning exercise. A verb gets a class, not bespoke limits.

## 8) Non-Functional / Constraints

- **Performance:** the layer adds under 5 ms to a call that fits its budget; a refusal returns in under 50 ms.
- **Security:** limits and connection details are never request parameters; every limit change is recorded; identity comes only from the gate.
- **Compatibility:** a caller that relied on the oldest 5,000 points of a wide window will see an even sample instead. This is announced and covered in the release notes, and the old behavior is not kept behind a flag.
- **Operability:** the policy table is configuration an operator can change on a live node; the suspend switch needs no deploy.
- **Domain-agnostic:** nothing here names a consumer. Consumers contribute cost-class declarations and channel metadata, and never limits.

## 9) Open decisions for the owner

1. **The numbers.** The first policy table needs defaults and ceilings by cost class and caller class. I propose starting an MCP agent at 200 points and 64 KB by default, and 2,000 points and 256 KB at most; a service at 5,000 points and 4 MB; the UI and CLI between them. These are proposals to be measured against real use, not settled values.
2. **Refuse or reshape, by verb.** The default is to reshape. Some verbs, such as an export, should refuse above a ceiling because a partial file is worse than none. Which verbs are those?
3. **Who owns the policy table.** It is an operational surface with security consequences. It needs a named owner and an approval path for raising a ceiling.
4. **Phase 0 on production.** Setting role-level limits changes the production database, and a limit set too tight could fail a legitimate long job. I would set them from measured use, apply them to the serving roles first and the maintenance roles separately, and stage them with an alert before enforcing.
5. **The export-control gate's timing.** Today it classifies after the query runs. Should a controlled channel or window be refused before execution? It saves the cost and removes a side channel, and it needs the control list to apply to requests as well as results.
6. **The idle-in-transaction timeout for maintenance jobs.** A timeout would have bounded the six-hour transaction of 2026-10-05, and it would also kill a legitimate long job that goes quiet. Which jobs need a longer one, and do they commit in batches instead?

## 10) Evidence behind this document

- A colleague's harness refused a 459,096-character answer on 2026-10-02; the series call measured 327,015 bytes, 5,000 points, covering 500 seconds of a requested day.
- A 30-day aggregate was refused after the full 20 second timeout, measured 2026-10-02.
- Query shape cost measured 2026-09-24: 0.33 ms with the full index prefix, 977 ms missing one key column, and 7,262 ms for a latest-per-site view over about 70 million rows.
- The production database role settings, server defaults and enforcement flags were read on 2026-10-05: no role-level limits on any serving role but one, `max_connections` 100, no temporary-file limit, enforcement unset.
- A six-hour transaction on 2026-10-05 held exclusive locks on the main gold table, blocking an anti-wraparound vacuum and a query for hours; it was one maintenance job, and it showed how little separates the serving tier from the jobs that run beside it.
- The per-verb inventory, with a file and line for each CODE claim, is kept by the consumer layer that owns the verbs, since it names them.

## 11) Acceptance & rollout

- **Sign-off:** the owner approves the policy table and the Phase 0 settings; the platform lead approves ADR-157.
- **Rollout:** Phase 0 settings go to the serving roles first behind an alert-only period; Phase 1 ships the layer in report-only mode (it logs what it would have limited) before it enforces; Phase 2 enables quotas one caller class at a time, anonymous first.
- **Rollback:** every phase is a configuration change or behind one, so rollback is a setting and not a release.
- **Done when:** the success metrics above hold for four consecutive weeks and the battery has failed a build at least once on purpose.

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
