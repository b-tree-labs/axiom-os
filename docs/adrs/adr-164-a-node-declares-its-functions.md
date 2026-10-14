# ADR-164: A node declares its functions, and the installer installs exactly those

**Status:** Proposed (2026-10-06)
**Related:** [ADR-019](adr-019-node-profiles.md) (node profiles, the hardware axis this sits beside), ADR-050 (tenant and site vocabulary), [ADR-106](adr-106-push-ingest-gateway.md) (the ingest face a transmitting node posts to), [ADR-131](adr-131-there-is-always-a-medallion.md) (every install has a medallion; the hosted form), [ADR-153](adr-153-a-role-is-granted-at-a-site.md) (roles are granted at a site), [prd-managed-infrastructure](../prds/prd-managed-infrastructure.md), [prd-signal-ingest-and-producer](../prds/prd-signal-ingest-and-producer.md), [prd-access-and-onboarding](../prds/prd-access-and-onboarding.md), [spec-embodied-surfaces](../specs/spec-embodied-surfaces.md) (the command path a control function hosts)


**2026-10-08 (ADR-180):** contributor mode and local-first mode are two function sets of the same producing node; switching between them is a function change under this ADR, never a reinstall. The local medallion's archive role is a function set too: bronze on; silver, gold, web app and chat installed but off.

## Context

A site in this platform is often more than one node, and the nodes do
different jobs. Two shapes are now deployed side by side.

- **A partner site node** acquires data from local instruments, holds it in a
  spool, and transmits it to another site's ingest face. Later it may also
  run a local command path. It has no data platform, no web app and no
  assistant, and it accepts no inbound connection.
- **A host site node** runs the ingest face that receives those
  transmissions, the data platform that conforms and serves them, the web app
  people sign into, and the assistant that answers questions over the data.

Nothing in the platform can say which of these a node is.

[ADR-019](adr-019-node-profiles.md) gives every node a **profile**: Edge,
Workstation, Server or Platform. A profile is a statement about hardware. It
answers "how large a stack can this machine carry", and it says nothing about
which jobs the machine has been given. A Server-class machine can be a pure
acquisition node, and a modest machine can host a web app for a small group.

Consumer layers have filled the gap with a single bit. A site configuration
records a hosting posture of "tenant" (post to another site's ingest) or
"self-hosted" (run your own node and medallion). One bit cannot describe a
node that acquires and transmits today and will add a command path next year,
and it cannot describe a host node that runs ingest and the data platform but
not the assistant. Every installer decision that hangs off that bit is a
guess about the function set it stands for.

The cost of the guess is visible at install time. An installer that does not
know a node's functions installs a general stack and leaves the operator to
work out what is running. At recent partner installs, operators found
scheduled services on their machine that nobody had explained, several of
them described as agents, and the first question was what those agents were
doing on an acquisition box. The honest answer was that most of them should
not have been there. A service that is not needed is a service somebody has
to trust, patch and explain, and a partner site's review will ask about each
one.

## Decision

A node declares the **functions** it performs. The installer installs the
services of exactly those functions, explains each service in plain service
language, and refuses a function whose prerequisites are missing. The
function set is a second axis beside the ADR-019 profile: the profile says
how large a stack the machine can carry, and the function set says which
jobs it has.

### 1. The declaration

The node's configuration carries one list:

```toml
[node]
functions = ["acquire", "transmit"]
```

The vocabulary is fixed in Axiom and has seven members.

| Function | What the node does | Axiom mechanism it installs |
|---|---|---|
| `acquire` | Reads data from local sources and holds it in a durable local spool. | The producer and its source adapters ([prd-signal-ingest-and-producer](../prds/prd-signal-ingest-and-producer.md)). |
| `transmit` | Sends spooled data to another site's ingest face, outbound only, with no listener on the node. | The producer's transport to an ADR-106 ingest face, with a site-scoped key read from the vault. |
| `control` | Accepts command intents, checks them against a declared envelope, routes accepted commands to local actuators and journals every decision. | The command path in [spec-embodied-surfaces](../specs/spec-embodied-surfaces.md): intent, `CommandRouter`, `EnvelopeCheck`, `Interlocks`, command journal. |
| `ingest` | Receives transmissions from other nodes and lands them in bronze. | The ADR-106 ingest face. |
| `data_platform` | Holds the medallion, runs the conform pass and scheduling, and serves gold. | The data platform in its embedded or shared form (ADR-131 §2). |
| `front_end` | Serves the web app people sign into. | The serving face and the appkit node shell. |
| `assistant` | Answers questions in chat over the node's data and corpus. | Chat, retrieval and model routing. |

Axiom owns the vocabulary, the mechanism and the installer contract. A
consumer layer supplies what each function means for its own domain: which
source adapters `acquire` runs, which declarations `control` checks commands
against, which brand and surfaces `front_end` shows. A consumer layer may add
services to a function. It may not add a function, because a function a
second consumer cannot use is a consumer concept wearing a substrate name.

### 2. A function owns its services, and nothing else is installed

Every service, timer and container the installer can place belongs to
exactly one function. Extensions declare the function their services belong
to in their manifest. The installer installs the union of the services owned
by the declared functions and nothing else, and it starts nothing that is not
installed.

A service that belongs to no function is an installer error, not a default.
This is what makes the declaration true rather than advisory: if a service can
arrive without a function, the function list stops describing the node the
first time somebody adds one.

### 3. Prerequisites are checked, and a missing one is a refusal

Each function declares what must be true before it can be installed. The
installer checks every prerequisite before it installs anything and refuses
with a message that names the missing piece and the command that supplies it.

| Function | Refused unless |
|---|---|
| `transmit` | a target ingest face is configured, a site-scoped key for it resolves from the vault, and the target answers an authenticated reachability probe. |
| `acquire` | the node also declares `transmit` or `data_platform`, so acquired data has somewhere to go. |
| `control` | the node also declares `acquire`, and a command envelope is declared. An empty envelope refuses every command, which is the correct behaviour and is reported as such. |
| `ingest` | the node also declares `data_platform`, so received data has a medallion to land in. |
| `front_end` | the node also declares `data_platform` or `assistant`, so the app has something to serve. |
| `assistant` | a model route resolves, local or remote. |
| any function | its services fit the node's ADR-019 profile; otherwise the installer refuses and names the profile promotion that would allow it. |

A refusal installs nothing. A partially installed function is worse than an
absent one, because it is running and cannot do its job.

### 4. Every installed service is explained as a service

The install output and `axi status` list each installed service with the
function it belongs to and four plain facts: what it does, when it runs, what
it reads, and where it writes or sends. A service that reasons with a model
is described the same way as any other scheduled service, in the way a
person would describe a cron job:

> `spool-health` (transmit) runs every five minutes. It reads the local spool
> and sends nothing. If the spool has stopped draining, it raises an alert on
> this node.

Agent persona names do not appear on that surface. A service is named by
what it does. Whether a service is agentic is a fact about its
implementation, recorded in its manifest and visible on request, and it is
never the headline. This is a requirement on the installer's wording, and it
has a test.

### 5. A node without `data_platform` still has a medallion

ADR-131 says every install has a medallion. That holds here, and the
function set says where it is. A node that transmits and does not declare
`data_platform` uses the **hosted** form: its medallion is the one on the
node it transmits to. Its local spool is a buffer with a declared retention
window. It is not a medallion, and nothing is served from it.

### 6. The hosting-posture bit is derived, and kept as a deprecated alias

A consumer layer that records a hosting posture today derives it from the
function set:

- a node that declares `transmit` and not `data_platform` is a **tenant** of
  the site it transmits to;
- a node that declares `data_platform` is **self-hosted**.

During migration the loader reads the old posture key, maps it to a function
set, writes nothing, and prints a deprecation notice that shows the
equivalent `functions` line. A configuration that carries both, and in which
they disagree, is refused. The alias is removed once every known site
configuration has moved.

### 7. Changing the set is install and uninstall, never a rebuild

Adding a function installs its services. Removing one uninstalls them. This
is the same move as ADR-019's promote and demote, on the other axis.
Removing `data_platform` from a node that holds data is refused until the
operator states what happens to that data, because the installer cannot
infer a disposition.

A node configuration with no `functions` key is an existing install. The
installer keeps today's behaviour for it, prints the function set it
inferred, and offers the line to commit. New installs always write the
list explicitly.

## Options considered

**Keep the posture bit.** It is already in every partner site's
configuration and the scaffolder already emits it. It lost because one bit
stands in for a set of seven independent facts, so every installer decision
made from it is a guess. It also cannot represent a node that adds a command
path, which is the next change already planned.

**Extend the ADR-019 profiles.** A profile could grow function-shaped
variants ("Edge acquisition", "Server host"). That lost because size and
function are independent. Folding them together turns four profiles into a
product of profiles and function combinations, each needing a name, and it
breaks ADR-019's promote and demote, which change size without changing
jobs.

**Per-service toggles with no grouping.** The installer could expose every
service as an individual switch. That lost because an operator reasons about
jobs, not about the twenty units behind them, and because a flat list of
toggles cannot carry prerequisites that span services.

## Consequences

**What gets easier.** An installer can tell an operator what they are
installing, in terms they can check against what they asked for. A partner
site's review sees a short list of services, each with a stated purpose,
instead of a general stack. A host node and a partner node are two
declarations of one vocabulary, so documentation, tests and status output
describe both without special cases.

**What gets harder.** Every service must be assigned to a function, and
existing services that were installed by default need an owner. Some will
turn out to belong to no function, and those are removed rather than
grandfathered. The installer gains a prerequisite engine and a refusal path
that must be tested for each function.

**What we are committed to.**

- The seven-function vocabulary is Axiom's, and consumer layers fill it in
  rather than extend it.
- A service with no function is an error.
- The install surface describes services in service language, and that
  wording is tested.

**Follow-up work.**

1. Add the function field to the extension manifest and the function
   registry to the managed-infrastructure spec.
2. Build the prerequisite checks in section 3, each with a refusal test.
3. Add the per-service explanation to install output and `axi status`, with
   a test that no persona name reaches it.
4. Assign every existing default service to a function, and remove the ones
   that belong to none.
5. Add a hygiene check that fails when a running unit is owned by no
   declared function.
6. Consumer layers migrate their posture key to `functions` and publish what
   each function means in their domain.
