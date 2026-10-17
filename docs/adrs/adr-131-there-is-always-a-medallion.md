# ADR-131 — There is always a medallion, and every surface enters through it

- **Status:** Proposed — 2026-09-24
- **Context owner:** data platform / install
- **Supersedes nothing.** Answers a question ADR-128 deliberately left open.

## Context

ADR-128 settles which tier a record belongs in and what each tier may do to
it. It assumes a medallion exists. This ADR decides whether that assumption
holds in every install, and what happens to content that arrives through a
product surface rather than through an ingest connector.

Two things forced the question at once.

**Install modes.** Three are first-class: chat only, chat plus the data
platform, and chat plus the data platform plus domain packs. Read naively,
that reads as a menu in which the medallion is the thing you add in mode
two. If that is true, then mode one needs somewhere else to put content,
and whatever that somewhere is becomes a second data architecture that has
to be kept honest separately.

**Serving surfaces receive content.** A webapp accepts an upload and a form
submission. A CLI accepts a file path. An MCP tool accepts a payload. Each
of those surfaces already holds a database handle, because it has its own
store. So the shortest path from "a file arrived" to "the user sees a
result" is a parsing function in the surface tier, next to the handle.

That shortest path is the failure mode. It is not hypothetical here: the
same freshness answer was computed two ways in this repo, and the worse of
the two was the one in use. Transformation written beside a serving handle
is transformation that no other consumer can reach, cannot be replayed,
carries no provenance stamp, and disagrees with the tier version the first
time either changes.

**What the node already shows.** `bronze.ingested_files` is a shared
arrival record, not a per-source one. It carries identity, origin,
content type, size, `content_sha256` and arrival time, and its rows span
content classes: 3,325,920 line-delimited JSON artifacts from acquisition
ingest alongside PDFs, images and text registered under other tiers. The
mechanism for "content arrived, here is its hash and where it came from"
is built and in production.

It is also unevenly used. Of 21,846 documents in the retrieval corpus,
about 3,423 have an arrival row. The rest were written straight to the
corpus by an ingest path that never registered them. That is the drift
this ADR names, and it is the same drift in a different tier as the ones
ADR-128 found.

## Decision

### 1. Every install has a medallion

Not "the data-platform install mode adds a medallion." Every install has
one, including a chat-only install. What varies between installs is the
medallion's deployment form. Its presence does not vary, and neither does
the discipline.

The chat-only install is the case that decides this. Its retrieval corpus
is content that arrived from somewhere, was transformed into chunks and
embeddings, and is served to a reader. That is bronze, silver and gold
whether or not anyone wrote the words down. Declaring the medallion absent
in that mode does not remove the three stages; it removes the names, the
provenance, the replay and the single place a fix lands.

### 2. A medallion is a discipline, not a size

Three deployment forms, all first-class, none of them a lesser medallion:

| form | what it is | who it is for |
|---|---|---|
| **embedded** | three schemas in the install's own Postgres | a single-node install, and what runs on the prototype node today |
| **shared** | a medallion on a node that several installs are served by | a group with one substantial machine |
| **hosted** | a managed service the install is a tenant of | an install that wants no data infrastructure of its own |

An embedded medallion may be three schemas and a handful of tables in one
database. That is a complete medallion. The tiers are how data is kept
organized and how the work is kept in one place; they are not a statement
about cluster size, and nothing in ADR-128's rule needs more than schemas
to hold.

The form is a deployment choice recorded in the install's configuration.
It is not a code path: the writers, the conform pass and the serving reads
are identical across all three, because the tier contract is what they
target, not the topology underneath it.

**Separate databases are the production shape.** Co-locating the platform
store and the medallion in one Postgres instance, as the prototype node
does, is a property of that node and not the architecture. ADR-129 governs
the boundary between them, and it holds in the co-located case precisely
so that splitting them later is a configuration change rather than a
rewrite.

### 3. Content enters through the medallion regardless of which surface received it

Webapp upload, web form, CLI argument, MCP tool call, connector poll,
federation peer push. Every one of these is a **receiver**. A receiver
authenticates, authorizes, validates the request shape, and hands the
content to the medallion. It does not parse it, clean it, unit-convert it,
resolve its identity or derive anything from it.

Which tier the content enters at is decided by ADR-128's test — what must
happen to the data — and never by which surface received it:

- **An artifact with a raw form** (an uploaded file, a pasted document, a
  pushed batch) lands in **bronze**. Bronze records its arrival: identity,
  origin, content type, size, content hash, arrival time. Silver does the
  twelve things to it.
- **Authored content with no raw form** (a form field, a curator label, a
  configuration a person typed) enters where ADR-128's allowances E1 and E2
  put it. Forcing it through bronze invents a raw landing that does not
  exist and a conform pass that copies a row.

The surface's own store may hold the fact that a submission happened —
who submitted, when, its status — because that is the surface's own
operational state. It may not hold the submission's content as the
authoritative copy.

### 4. Raw bytes live in the medallion

An uploaded file's bytes are stored by the medallion's artifact store and
referenced from its bronze arrival row, the way `source_path` already
works. They are not stored in the webapp's filesystem, its container, or a
volume only it can see.

The reason is replay. The whole value of landing bytes as received is that
a transformation can be re-run against the original when the transformation
is found to be wrong. Bytes held by a serving tier are reachable only while
that serving tier is deployed, in that form, on that host.

### 5. Transformation does not live in a serving tier

This is ADR-128's rule stated for the surface that was not covered by it.
ADR-128 says gold derives rather than transforms. This says the tier above
gold does neither.

A serving tier may select, format, paginate, localize, render and cache.
Those change how an answer is presented. It may not clean, cast,
decompose, hydrate, resolve identity, deduplicate, compose, validate,
stamp provenance or reconcile. Those are silver's twelve, and doing one of
them in a serving tier guarantees the next surface that needs it will do
it differently.

Under ADR-129 a serving surface reads gold through exactly one declared
route. That is a route to an answer, not to a transformation seam.

### 6. What this means for the install modes

The modes remain three, and the difference between them is not whether a
medallion exists.

- **chat only** — a medallion exists and holds corpus content: arrival in
  bronze, chunking and embedding in silver, the served corpus in gold.
  Embedded form, small.
- **plus data platform** — the medallion gains acquisition ingest, the
  conform pass, scheduling and the full serving surface.
- **plus domain packs** — packs add tiers' contents, never tiers, and
  never a second path in.

An install that grows from the first mode to the second does not acquire a
medallion. It acquires more of one.

### 7. Federating does not create a second place to read

The case: a node runs unfederated, accumulates a substantial medallion of
its own uploads and derived content, then federates. Somebody uploads the
next day. Where does that content go, and is there now one copy or two?

An earlier draft of this section answered that local gold and shared gold
are "read as distinct, named sources." That is wrong, and it is worth saying
why, because the wrongness is subtle: naming two sources *labels* the
ambiguity instead of removing it. A reader still has two places an answer
could come from, and split brain is exactly what follows. ADR-129 already
forbids this one level down, where a serving surface reads exactly one
declared route and never falls back. The rule does not stop applying because
the second store is a peer's instead of an app's.

**7.1 A node serves from exactly one gold: its own.** There is no second
serving location, federated or otherwise. Whatever a node can answer, it
answers from its own medallion. This is the whole resolution to split brain,
and everything below is what has to be true for it to hold.

**7.2 Inbound federation is an ingest source, not a store to read.** A
peer's published content is a *producer*, in exactly the sense §3 means:
it enters the receiving node's medallion the same way an upload does.
Bronze records the arrival — which peer, when, what hash. Silver resolves
the peer's identifiers into this node's vocabulary, reconciles them, and
stamps origin provenance. Gold derives. Peer content then lives in one
place, this node's gold, told apart by a provenance column rather than by
living somewhere else.

This is the correction to the earlier draft. Federation does not hand a
reader a second gold. It hands a node a new source, and sources are a thing
the medallion already knows how to absorb.

**7.3 Direction is preserved.** The peer pushed and this node received.
Nothing reached into the peer, so the rule that site edges push out and
nothing reaches in is intact in both directions of a federation link.

**7.4 Outbound publication is a separate, declared act.** Joining a cohort
grants eligibility to publish, not publication. A publication scope is
declared explicitly, and until it is, peers receive nothing. Otherwise
joining is a bulk disclosure of everything accumulated before anyone decided
it was shareable, at the one moment nobody is auditing it.

**7.5 Publication carries withdrawal.** A published record can be
withdrawn, and the withdrawal travels the same path the publication did, so
a receiving node applies it. Without this, "unpublish" is not a possible
operation once anything has been ingested by a peer, and the first mistaken
publication is permanent across the cohort.

**7.6 Freshness of peer content is a property, not a hidden cost.** A
node's copy of a peer's material is as current as its last ingest, and that
is visible the way any other freshness is. It is deliberately not a live
cross-node query: that would put a peer's availability and latency on this
node's serving path, which is the coupling §7.1 exists to prevent.

**7.7 The receive path is byte-identical before and after federating.**
Unfederated: receive, bronze, silver, gold. Federated: receive, bronze,
silver, gold, and then a separate publication step may run against gold. The
surface that accepted the upload does not know or ask whether the node is
federated. If a receiver has to branch on federation state to decide where
content goes, the design is wrong, and this is the rule that says so.

**7.8 Pre-federation content does not move.** It is already in local gold.
Federating makes it *eligible* for publication under §7.4 and relocates
nothing. De-federating stops publication and nothing else, because local was
complete the whole time. Leaving a cohort is not a restore.

### 8. Who holds a medallion, and how content gets in without a web server

§1 says every install has a medallion. Two things follow that are easy to
misread, and both were raised as objections to §7.

**8.1 The medallion belongs to the install, not to the person.** A browser
is not an install. Neither is an MCP client, nor a CLI pointed at a remote
node. A person working through any of those holds no medallion and is not
expected to, and nothing in §1 says otherwise.

What a person is a client *of* is one node per request. That node is where
the request is served. It is **not** the answer to "what may I see" — see
§10, which is the rule that governs scope.

**8.2 A receiver is not a web server.** §3 lists the CLI and MCP as
receivers alongside the webapp, and that is what makes an unfederated,
server-less install work. An operator with no web server running adds
content through the CLI or an MCP tool, and it lands in the local
medallion's bronze exactly as an HTTP upload would. Nothing about the
content path requires HTTP.

So the three cases are distinct and none of them is ambiguous:

| the person has | their medallion is | they add content via |
|---|---|---|
| a local install, no web server | local, embedded | CLI or MCP |
| a local install with the web app | local, embedded | any receiver, including upload |
| only a browser | the node they are a client of | that node's receivers |

### 9. A derived index federates the same way, and the shared one is never a superset

A retrieval index is a consumer of medallion data, so it inherits §7 rather
than needing its own rule. But it is where the pressure to break §7 is
strongest, because a cohort's shared index is bigger than a single node's
and "just use the better one" is an attractive sentence.

The case: a node runs unfederated, builds up a local index over its own
material, then federates into a cohort with a far larger shared index.

**9.1 The shared index cannot be a superset, by construction.** Publication
is opt-in (§7.4), so a node's unpublished material is *absent* from the
cohort's index and always will be. A shared index is therefore bigger and
incomplete at the same time. Switching to it does not trade a small corpus
for a large one; it trades a complete view of your own material for a
partial one. Any design that presents "local or shared" as a preference
gets this exactly backwards.

**9.2 The local index is never discarded, and never replaced.** It holds
the material the node is accountable for. Reach is added to it; it is not
swapped out.

**9.3 Two mechanisms add reach, and which one applies is a node
configuration, not a user choice.**

- **Absorb** (the default, and just §7.2 applied to an index): peer-published
  content ingests into this node's medallion, so this node's index grows to
  include it. One index, one place, origin carried per document. This is
  right whenever the cohort's published material is a size the node can
  hold.
- **Delegate**: the node does not hold a peer's corpus and asks the peer's
  retrieval service instead. This is right when the shared corpus is large
  enough that absorbing it is absurd, which is the case the question is
  really about.

**9.4 Delegation is not a second serving location, provided the delegation
belongs to the node and not to the reader.** The reader asked one node. That
node owns the answer. Whether it satisfied the request from its own index or
by asking a peer is its implementation, in the same way that a query plan is.
What makes this honest rather than a loophole is three conditions, and
delegation without them is the split brain this ADR refuses:

1. **Each result carries its origin.** A reader can always say where a
   passage came from, not by having chosen a source beforehand, but because
   the answer says so.
2. **The node decides, not the user.** There is no surface control that
   reads "search local or shared." A reader who can pick has two serving
   locations again, whatever the code looks like.
3. **An unreachable source makes the answer narrower and the answer says
   so.** This is ADR-129's staleness rule in mirror image: a stale projection
   is reported as stale and never silently backfilled, and here a partial
   retrieval is reported as partial and never silently returned as complete.
   Quietly returning three results instead of thirty because a peer was down
   is the failure mode delegation actually has, and it is invisible unless
   this is enforced.

**9.5 Peer-origin material is a distinct corpus within the one index, not a
distinct index.** The corpus axis already exists and retrieval already
filters on it, so a peer's published material is absorbed as its own named
corpus. That keeps origin recoverable at read time, makes withdrawal (§7.5)
a scoped operation rather than a search-and-delete, and means a surface that
should not read peer material declines it by naming its corpora, which is
the mechanism that already governs what a served surface may retrieve.

### 10. The door does not determine the answer: federation determines scope, an endpoint only serves

The case: a person runs a local node for a while, accumulating their own
material. Then they log in to the shared site's web app, carrying the same
identity. Their chat there should be able to answer from their local
material, or their experience fragments into "what I can see depends on
which door I walked through."

That requirement is correct, and it forces an invariant stronger than
anything above:

> **A request's scope is a function of identity and federation topology. It
> is never a function of which node received the request.** The endpoint is
> a serving mechanism. Federation is the scoping mechanism.

Everything in this section is what has to be true for that to hold.

**10.1 The shared node does not reach back into the local node.** It cannot:
a personal node is asleep, behind NAT, or offline most of the time, and
putting it on a shared node's serving path makes every answer depend on a
laptop being awake. It also inverts the direction rule that edges push out
and nothing reaches in. Any design where the shared resource pulls from a
personal node is rejected on both grounds.

**10.2 Scope converges because material replicates along identity edges, not
because anyone queries across them.** The local node *pushes* the owner's
material to the nodes that owner uses, scoped to that owner. When they log
in to the shared web app, their material is already there, because their own
node put it there. Nothing reached back.

**10.3 Personal replication is a second publication scope, distinct from
cohort publication.** §7.4 covers publishing to peers, which is sharing.
This is publishing to yourself on the nodes you use, which is not sharing
and must not be conflated with it:

| scope | what it means | who can then read it |
|---|---|---|
| cohort publication (§7.4) | shared with peers | the cohort, per its policy |
| personal replication (§10) | follows its owner | that identity, and no one else |

Collapsing these is the dangerous mistake, because it turns "I want my notes
on my laptop and in the browser" into "I published my notes to the cohort."

**10.4 The node an identity replicates to is a federation fact, not a
setting.** Which nodes a person uses is exactly what their memberships say,
which is ADR-130's territory: a membership names a site, credentials name one
active site, and scopes fan out explicitly rather than merging. Replication
follows those edges and no others.

**10.5 Personal material on a shared node is tenant-scoped, and that is the
existing mechanism.** Replicated personal material lands in that identity's
own corpus on the receiving node, and a served surface reads the community
corpus plus that identity's, so another user of the same shared node cannot
retrieve it. This is not new machinery invented for federation — it is the
retrieval isolation axis, and it is what makes 10.2 safe rather than a
disclosure.

**10.6 Convergence is eventual, so a node must be able to say it is
incomplete.** Two doors give the same scope only once replication has caught
up. Before that they differ, and the invariant would be a lie if a node
answered narrowly in silence. So a node states how much of an identity's
entitled scope it is actually serving, the way §9.4 requires of a delegated
retrieval and ADR-129 requires of a stale projection. A narrower answer is
reported as narrower. This is the third occurrence of the same rule, and it
is the one that keeps every convergence claim in this ADR honest.

**10.7 §7.1 still holds and is not in tension with this.** A node still
serves from exactly one gold, its own, and still never reads a second store
at query time. What §10 adds is *why* one node's gold is the right thing to
serve a given identity from: because the material an identity is entitled to
has been replicated into it. Scope is decided by federation; serving is
decided by whichever node is in front of the person.

### 11. Content created ON a shared node, and how it gets home

§10 described a personal node pushing its owner's material outward. It did
not cover material that *originates* on the shared side, and that is the
ordinary case: someone logs in to a shared node's web app, is mid
conversation, hits plus and drops a file in. Which medallion takes it, which
index updates, and how does the personal install ever see it?

**11.1 It lands in the receiving node's bronze.** §3 and §7.1 leave no
choice: the shared node is the receiver, and a receiver puts content in its
own medallion. It cannot put it in the personal node's, which may be asleep,
and reaching toward a personal node is what §10.1 refuses.

**11.2 It is indexed into the uploader's tenant corpus on that node, and
promptly.** The answer to "which RAG" is the shared node's, in that
identity's corpus. Promptly because the very next question in that
conversation is about the file just dropped, so an indexing cadence measured
in minutes is a broken feature rather than a tuning choice.

**11.3 The origin of that artifact is the shared node.** §7.5 is unchanged
and needs no exception: origin is a property of the artifact, not of what
class of node holds it. The personal install will hold a replica, and
conflicts resolve toward the shared node for this artifact exactly as they
resolve toward a personal node for one uploaded there.

**11.4 The personal node is the active party in both directions.** A shared
node has a stable address and is always up. A personal node is asleep,
behind NAT, or closed. So the personal node pushes what it owns *and* pulls
what it owns, and the shared node never initiates toward it.

This is not a violation of the rule that edges push out and nothing reaches
in. That rule keeps the outside from reaching *into* a protected site. A
member's own install fetching its own material from a node it is a member of
is an ordinary outbound client request, and the protected-site direction is
untouched by it.

**11.5 Personal replication carries the artifact, not only the derivation.**
This differs from cohort publication (§7.2), which crosses as derived gold
and never as raw bytes, and the difference is deliberate: in personal
replication the owner is the same on both sides, so there is no disclosure
boundary to defend. A replica holding only gold also cannot re-derive when a
transformation is later fixed, which would make the personal copy
permanently worse than the shared one. Where volume makes full replication
unwanted, that is a declared policy, and §10.6 then requires the install to
say what it is not holding.

**11.6 An attachment's scope is declared, not inferred.** Dropping a file
into a conversation can mean "use this to answer me here" or "keep this."
Those are different, and if nobody decides, the implementation decides by
accident. **The default is conversation-scoped: an attachment is retrievable
within its conversation and is not added to the identity's corpus until an
explicit act promotes it.** The reverse default is the surprising one,
because it means every file anyone ever dragged into a chat is permanently in
their retrieval corpus, surfacing in unrelated answers months later, with no
moment at which they agreed to that. Promotion is cheap and reversible;
un-ringing the other bell is neither.

Only a promoted attachment replicates under §11.4. A conversation-scoped one
travels with its conversation.

### 12. Sharing changes visibility; visibility is what retrieval reads

§11.6 makes an attachment conversation-scoped by default and promotable to
its owner's corpus. Promotion is not sharing. This section is the other axis:
who besides the owner may retrieve a document, and what has to be true for
that to be safe.

**12.1 The Library is a serving surface, and "as-uploaded" is a fact bronze
already records.** An uploaded artifact appears in the Library under an
as-uploaded folder or tag. That is not a new store: the arrival row already
carries display name, source name, content type, size, content hash and
arrival time, and "arrived by upload" is a property of how it was received.
The Library selects, filters and renders those records. Under §5 it does not
transform them.

**12.2 Sharing does not move or copy a document between corpora.** The
document stays in its owner's corpus and gains a **grant**. Moving it would
take it out of the owner's own retrieval; copying it would create a second
artifact that revocation has to chase. One document, one home, a set of
grants against it.

**12.3 The visibility ladder, and who may set it.** Private to the owner is
the default that §11.6 establishes. Above it: shared with a person, shared
with a group, public to the site. Only the owner moves a document up or down
that ladder, and each step is an act with a record, because each step is a
disclosure.

**12.4 Corpus stays the coarse partition; a grant is row-level.** This is an
honest evolution of the tenant-corpus work rather than a restatement of it.
Corpus remains the cheap first filter and the tenancy boundary: community,
the identity's own, the site's public one, and a corpus per group works
naturally because group membership is knowable before the query. Sharing
with an individual does not: minting a corpus per share-set is combinatorial,
so a per-person grant is a predicate on the document, not a corpus name.

The consequence for retrieval is that it must take **a reader identity**, not
only a list of corpus names. A reader sees documents in the corpora they may
read, plus documents granted to them. Absent an identity it must fail closed
and return the community corpus alone, never everything.

**12.5 Revocation is immediate; a grant may be eventual.** The asymmetry is
forced and is the most important rule in this section. If a grant takes a
minute to reach the index, someone waits a minute. If a *revocation* takes a
minute, a document the owner just un-shared is still being retrieved into
somebody else's conversation, and no one can see that it happened. So a
visibility reduction is applied on the serving path at once, and only a
visibility increase is allowed to arrive through the ordinary indexing
cadence.

**12.6 Material shared WITH an identity does not replicate to that
identity's personal node.** §11.5 says personal replication carries the
artifact. It means material the identity **owns**. A document merely shared
with someone must not land on their laptop, because a replica that is offline
cannot be reached by §12.5's revocation, and "un-share" would become a thing
that works only against people who happen to be online. Shared-with-me
material is read at the node that holds it.

This is the one place where the unified experience of §10 is deliberately not
achieved, and the reason is that the alternative is an un-revocable copy. An
install states that it is serving less than the full scope, which §10.6
already requires of it.

### 13. Material that predates federating, seen from a shared node's Library

The case: someone used a local install for months, associating files with it.
They then federate and open the Library in the shared node's web app. What
does it show?

**13.1 Nothing appears automatically.** §7.4 and §7.8 already settle this:
joining grants eligibility, not publication, and pre-federation content does
not move. That holds even though personal replication (§10.3) targets the
owner's own scope and is readable by nobody else, because "my files are now
on a server I do not run" is a real change and it is the owner's to make, not
a side effect of signing in.

**13.2 The catalog and the content replicate separately.** This is the
distinction that makes a unified Library possible without a bulk upload, and
it is the substance of this section:

| what | consists of | size | is it a disclosure |
|---|---|---|---|
| **catalog entry** | name, type, size, content hash, origin node, arrival time | tiny | yes, but only of the fact and the name |
| **content** | the artifact plus its derived index entries | large | yes, of the material itself |

Replicating a catalog entry does not make a document retrievable. It makes it
*visible as existing*, which is what a Library is for.

**13.3 Three states, and the Library says which one an entry is in.**

| state | the Library shows | retrievable there |
|---|---|---|
| **replicated** | the entry, normally | yes |
| **catalogued** | the entry, marked as living on its origin node | no, with one action to bring it over |
| **withheld** | nothing at all | no |

This is §10.6 made concrete. A node that holds a catalog entry but not its
content says so on the entry rather than silently omitting it, which is what
lets someone see their whole working set from a shared node without that node
holding all of it.

**13.4 Federating asks once, with the scope visible.** Neither silent default
is acceptable: an automatic upload is 13.1's disclosure, and an empty Library
makes the unified experience of §10 a claim the product does not honour. So
the federation act itself carries a one-time choice about existing material,
made when the person is paying attention and can see the size of what they
are deciding about.

The choice is **scoped, not binary**, along the folder and tag structure the
Library already has. "These collections follow me, these stay here" is the
decision someone can actually make; "all or nothing" is not.

**13.5 A catalog entry is itself a disclosure, so withheld is a real state.**
File names leak. A local install may hold material whose existence and naming
should not leave the machine, and for that material the shared node is told
nothing rather than shown a placeholder — a greyed-out row that says
"something is here you may not see" is still a leak. An install that withholds
reports how much it is withholding in aggregate, per §10.6, without naming any
of it.

**13.6 Origin does not change, and a replica outlives its origin.** The local
node stays authoritative for material it originated, per §7.5 and §11.3. If
that machine is never online again, the replicated copies on the shared node
remain usable; they simply stop receiving updates. Losing the origin costs
currency, not access.

### 14. Enablement is guided, and the obligation it creates is continuing

§13.4 puts a one-time choice at the federation act. That understates what is
being decided: enabling sync is an enrollment in an ongoing obligation, and
both halves need to be honest.

**14.1 Enablement is guided, and the benefit is named.** People do not enable
something described only as a disclosure. The flow says what they get —
material reachable from any door, a working set visible from a shared node,
and durability that a single machine does not have — alongside what leaves
and what stays. §13.4's scoped choice is the mechanism; this is the framing
that makes it a decision someone can make rather than a dialog they dismiss.

**14.2 A replica is not a backup unless it is one.** This is the claim most
likely to be made loosely and most damaging when it breaks. A faithfully
syncing replica propagates a deletion as faithfully as it propagates a
change, so if "backup" is offered as a benefit then something must retain
material the origin no longer has — retention independent of the origin,
stated in the same breath. If it does not, sync is durability against
*hardware loss* and not against *deletion*, and the flow says exactly that.
Selling the stronger claim and delivering the weaker one fails at the moment
someone is relying on it.

**14.3 The obligation is continuing, and it is bidirectional.** Enrollment is
not a copy. Material changes on both sides — edited locally, uploaded on the
shared node (§11) — so reconciliation runs in both directions for as long as
enrollment lasts, with the personal node the active party in both (§11.4).

**14.4 It rides machinery that exists. We do not build a third sync path.**
Two mechanisms already carry parts of this, and the distinction between them
is real:

| obligation | home | why |
|---|---|---|
| node to node replication of an identity's material | the memory sync engine | it already does origin-preserving provenance, echo suppression, fail-closed outbound gating, LWW conflict with the loser queued for review, and a managed service with lease and recovery. Its transport module is already the named node-to-node seam over the federation A2A hop. |
| a rendered document kept reconciled against an external store | the publishing lifecycle | it already owns render, content-gate, sign, upload and pull-and-reconcile against published copies. That is the last mile to a human-facing destination, which is a different job. |

What this ADR adds is the artifact and catalog shape from §13, not a new
engine. Writing a third reconciliation loop for medallion artifacts would
reproduce, badly, the echo suppression and conflict handling that already
exist and are tested.

**14.5 The gate runs on every sync, not only at enrollment.** §13.5's
withheld state and §12.5's immediate revocation are not enrollment-time
settings. They are decisions re-made each time something would leave, at the
fail-closed outbound gate the sync engine already routes through. A scope
narrowed after enrollment takes effect on the next reconciliation, and
material that has already left is withdrawn under §7.5, which is why
publication had to carry withdrawal.

## Consequences

**A gap becomes visible and countable.** Corpus documents without a bronze
arrival row are now a defect with a number: roughly 18,400 of 21,846 on the
node. Registering them is tractable because the arrival record already
exists and already carries a content hash.

**Upload handling gets one home.** The next surface that accepts a file has
somewhere to put it that is not its own directory, and the answer does not
depend on which surface it is.

**The chat-only install carries three schemas it did not before.** This is
the real cost. It is small — schemas and a few tables in a database the
install already has — and it buys the property that there is one content
path to reason about rather than one per mode.

**"Where do I put this?" stops being per-feature.** ADR-128 answers which
tier. This answers that there is always a tier to answer with, and that no
surface is exempt from asking.

## What this does not decide

- The artifact store's implementation. Object store, filesystem or large
  objects is a deployment concern per form, not an architectural one.
- The hosted form's tenancy model. It is named here as a first-class form
  so that nothing in the contract assumes local storage; its isolation
  design is its own decision.
- The backfill order for unregistered corpus documents.
- Whether the retrieval corpus's silver and gold stages get tier-named
  tables or stay under their current names. Naming follows; the stages are
  what this ADR asserts.
