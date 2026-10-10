# ADR-157: The serving tier bounds what any one call can cost

**Status:** Draft (2026-10-05)
**Related:** [prd-gold-serving-safeguards](../prds/prd-gold-serving-safeguards.md), [ADR-114](adr-114-mcp-authority-enforcement.md) (who is calling), [ADR-128](adr-128-medallion-tier-boundaries.md) and [ADR-131](adr-131-there-is-always-a-medallion.md) (what the gold tier is), [ADR-132](adr-132-sensing-faults-detect-carry-refuse.md) (faults travel with the data), [ADR-136](adr-136-uncertainty-is-a-platform-primitive.md) (uncertainty travels with the data), ADR-151 and ADR-153 (roles and site-scoped grants; in review), ADR-156 (the activity journal; proposed)

## Context

The gold tier serves measured and derived values to people and to agents through four doors: the CLI, MCP tools, HTTP routes, and the tools a chat agent calls. Today the size of an answer is decided by whichever verb author wrote the verb, and by nothing else.

Three things happened in the first week of October 2026 that make this a decision and not a wish.

On 2026-10-02 a colleague's harness refused a telemetry answer of 459,096 characters. The verb had no default limit. A request for one day of a 10 Hz channel returned 327 KB, narrowing the window to five minutes still returned 196 KB, and only an explicit `limit` helped. Worse, the 24-hour request returned the oldest 5,000 points, which span 8.3 minutes, or 0.58 percent of what was asked for. An agent that averaged them would have reported an eight-minute average as a daily one. The response carried `truncated`, `covered` and `resume_from`, so the API was honest; the failure was that the payload was too large for the consumer to read those fields.

On 2026-10-05 the database that holds the gold tier carried a six-hour transaction from a maintenance job. Every consumer that wanted the table waited behind it. That was an internal job, not a hostile caller, and it showed how little separates the serving tier from the work that other things do to it.

Both of those were accidents. The serving tier is also about to be reachable by people who are not us. Colleagues connect agents to it through MCP, a keyed chat API is being opened to programs, and the gate now issues per-person keys. An ignorant consumer will ask for everything, because nothing told them not to. A malicious consumer will ask for everything on purpose, many times at once. In both cases the harm lands twice: on the database, and on the consumer's own context window, where an answer too large to read is as bad as no answer.

The decision to make is where the limit lives. A limit that each verb author remembers to write is a limit that new verbs forget, and the 327 KB answer came from exactly that.

## Decision

The serving layer enforces what any one call may cost. Verb authors declare what a verb is; the layer decides what is allowed. No call to the gold tier is unbounded, and no ceiling is left to the caller's good manners.

The shape, in eight parts:

1. **Every verb declares a cost class.** The classes are point (one value), bounded range (a window of raw or near-raw values), aggregate (a reduction over a window), and scan (an open-ended read). Scan is not offered to interactive callers. The class is a property of the verb's declaration, read by the serving layer; it is not a comment.
2. **Every call has a default and a hard maximum, in points and in bytes, set by caller class.** Caller classes are the UI, the CLI, an MCP agent, and a service. An MCP agent gets a byte budget that fits a context window; a service gets a larger one. The table that maps cost class and caller class to defaults and ceilings is configuration, not code, so an operator can change it on a live node without a release.
3. **A call that exceeds its ceiling is reshaped, paginated, or refused, and never silently truncated to its oldest part.** Reshaped means an even downsample across the whole requested window, with the resolution stated. Paginated means a cursor to the next part. Refused means a typed error that names the cheaper call that would answer the question, for example the aggregate verb. A request for a day of data therefore returns a day, coarsely, instead of eight minutes, finely.
4. **The response says what was done.** Every bounded response carries what was requested, what was returned, the span actually covered, the resolution, whether it was reduced, and a cursor if more exists. These fields lead the response, so a consumer that stops reading early has already seen them.
5. **A reduced answer keeps the truth that reduction usually loses.** Downsampling returns the minimum, maximum, mean and count of each bucket, not the mean alone, and it carries fault codes and uncertainty through (ADR-132, ADR-136). A reading of 0.0 that is a fault code must not be averaged into a plausible temperature.
6. **Cost is estimated before the query runs.** The layer computes the window divided by the channel's native period, times the number of channels, and compares it with the budget. A call that cannot fit is refused before it touches the database.
7. **Callers have quotas, and the quota belongs to the verified principal.** Each principal has a rate limit and a concurrency cap; an anonymous caller gets the smallest. The identity comes from the gate (ADR-114), never from a header the caller can set. The serving tier reads through a database role of its own, read-only, with a statement timeout, a lock timeout, an idle-in-transaction timeout, a temporary-file limit and a connection cap, separate from the roles the ingest and conform jobs use.
8. **Use is metered and can be cut off.** Per-principal usage is recorded for the activity journal (ADR-156), and an operator can suspend one principal at once without a deploy. Wide-range questions are answered from rollups, so raw reads stay narrow by design and not only by rule.

All of this is enforced at the one door that the CLI, MCP, HTTP and chat tools all pass through, and it is proven by an abusive-consumer battery that runs in the release gate: an unbounded range, an enormous window, a parallel flood, hostile parameter values, and an anonymous caller, run against every verb.

## Options considered

**Per-verb limits written by each verb author.** This is what exists. It is inconsistent, it is forgotten for every new verb, and it produced the 327 KB answer. It lost because the failure is not rare; it is the default outcome of leaving the limit to the author.

**Rely on the database's statement timeout.** A timeout bounds time, not bytes. A fast query can return a million rows, and it can do so many times at once. It also does nothing for the consumer's context window, which is half of the harm. It stays, as one layer of the serving role, and is not enough on its own.

**Document good manners for consumers.** The ignorant consumer does not read the documentation, an agent has no manners to apply, and the malicious consumer reads it only to find the edge. It lost because it protects against no one who matters.

**Refuse everything above the ceiling.** This is the simplest rule and the worst experience. An agent that is refused cannot see a trend at all and will retry with a smaller window until it has made twenty calls where one coarse one would have served. Reshaping serves the intent and keeps the cost bounded, so refusal is the fallback and not the default.

**Give agents a read replica and stop there.** A replica protects the primary, which is worth doing and is complementary. It does not protect the replica from being overrun, it does nothing for the consumer's context, and it leaves the silent-truncation trap in place. It was not chosen as the answer, and it is not excluded as a layer.

## Consequences

Verbs must declare a cost class, so every existing gold verb gets a declaration, and a verb without one is refused by the layer. The inventory of today's verbs against this decision is the first deliverable of the PRD.

The meaning of truncation changes. A caller that relied on getting the oldest 5,000 points of a wide window will now get an even sample of the whole window. That is the correct behavior and a visible change, so it is announced and covered by the compatibility notes.

The policy table becomes an operational surface. Someone owns it, changes to it are recorded, and a ceiling raised for a single caller leaves a trace.

A reduced answer is only as good as the rollups behind it. Serving wide ranges from rollups depends on the compression and continuous-aggregate work on the gold tier, so the sequence matters: the layer and its limits come first, because they protect the tier on the day they ship, and the rollups make the coarse answers cheap later.

Quotas keyed to a verified principal make the keyed API and the gate load-bearing for protection, not only for access. An unauthenticated door anywhere in front of the gold tier defeats the quota, so the shim's open port and any other unauthenticated route to a gold verb are part of this work, not a separate one.

Follow-up work: the policy table and its loader; the cost-class declaration on every verb; the downsampling and cursor machinery; the serving database role; per-principal metering; the suspend switch; and the abusive-consumer battery in the release gate. The PRD sequences them.
