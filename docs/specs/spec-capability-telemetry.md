# Spec: Capability Telemetry and Discovery

**Status:** Living
**Related:** ADR-121 (computed grounding), ADR-117 (skill conformance steward),
ADR-072/073 (capability projection), prd-capability-discovery

## 1. Problem

A capability nobody reaches for is worth nothing, and the failure is quiet: the
assistant does the task the hard way and nobody notices there was a better path.

Two things were missing. Usage was invisible — every surface published a
telemetry projection and nothing stored it, so per-surface usage existed only for
the instant an event sat on the bus. And capability was undiscoverable — an
assistant learns what a system can do from its own instruction file, and that
file had never carried it.

## 2. Where this lives

At `invoke_capability`, the single dispatch chokepoint CLI, chat and MCP already
share, where a capability has the same identity on every surface (the protocol
name-mangling stops at the transport).

This placement is the design. Instrumenting a protocol server would have been the
same work once per protocol and would have left the surface most worth proving
out — a conversation — uninstrumented.

## 3. The projection

Every invocation publishes: capability name, principal, surface, outcome, error
count, latency, and a digest of the arguments.

**No argument or result value travels.** The digest is *computed* from the
arguments rather than accepted from a caller, so a caller cannot smuggle content
through by supplying its own. This holds at both boundaries — publication and
persistence — because a store is where a leak becomes permanent.

`chat` is a named surface alongside `cli`, `mcp` and `runner`. A refusal in chat
is read by a person mid-conversation, not by an operator at a prompt or another
agent over a protocol, and those are not interchangeable when someone later asks
where a capability was used. Chat keeps publishing its full payload to its own
per-agent bus — the renderer needs the content and that bus has a narrow blast
radius — and emits the redacted projection *in addition*.

## 4. The series

Append-only JSONL under the state dir, mirroring the action audit chain.

It works on a laptop with no database, and that is the requirement rather than a
convenience: most installs are that laptop. A measurement that only functioned
where Postgres does would miss the people the platform is for, and would flatter
us by sampling only the nodes we run ourselves.

Recording never raises. Telemetry is an observation about a turn, not part of
it — a full disk must not fail somebody's conversation. The subscriber runs
fail-closed-to-silent for the same reason: a raising subscriber puts the whole
payload back on the bus inside its error record, which is precisely how a
content-free design springs a leak.

### 4.1 Two topics, one record per invocation (added 2026-09-21)

Surfaces genuinely publish on two topics, so the store subscribes to both:

| Topic | Published by |
|---|---|
| `capability.telemetry` | chat (dispatches beneath the chokepoint, so it emits the projection itself) |
| `tool.post_invoke` | CLI and MCP, via the gateway inside `invoke_capability` |

Subscribing to only the first is what shipped first, and it meant **every CLI
and MCP invocation on every install was measured and then dropped**. The gap was
invisible because the one surface exercised in the store's own tests was the one
that worked.

**Invariant:** a surface publishes on exactly one of these topics per
invocation, per bus. It holds because chat's gateway dispatch goes to its
per-agent bus while its projection goes to the process bus. That is load-bearing
rather than incidental, so it has a guard test *and* a negative control proving
the double-count is real — if it broke, usage would inflate silently and a
capability would drop out of the discovery block after one real call.

### 4.2 Arming, and declining (added 2026-09-21)

Every surface published and **nothing subscribed**: `enable_capability_telemetry`
had no caller outside the tests, so the series was empty on every install while
the code that fills it was fully tested. A measurement that cannot produce data
is worse than none, because it reports as built.

Armed at three startup paths: the CLI entry point and both MCP servers. Each
call is idempotent, guarded, and never blocks the command or the server.

`AXIOM_CAPABILITY_TELEMETRY=0` (also `off`/`false`/`no`/`disabled`) declines the
series. Checked at subscribe **and** at every write, so a process armed before an
operator declined stops writing rather than appending for its whole lifetime.
The default is on: the series is content-free and never leaves the machine, and
the honest numbers we most need come from installs we do not administer. It is
still theirs to decline — a measurement with no off switch is one somebody has
to uninstall the platform to escape.

`AXIOM_STATE_DIR` relocates where the series and the block read from. It is
operationally useful, and it is also what makes anything reading the series
testable: without it, a test that built an MCP server rendered a block from the
developer's own `~/.axi`, so its output varied by machine and by what that
person happened to run that week.

## 5. What "discovered" means

Deliberately stricter than "appears in the series". Adversarial stress broke the
weaker definition four ways, each of which let the loop look healthy while
measuring nothing:

| Rule | The failure it prevents |
|---|---|
| **Per principal** | A colleague's usage emptied the block for a newcomer — the person it exists for |
| **Repeat use** | One stray invocation hid a capability permanently |
| **Decay window** | A one-way ratchet cannot re-surface something nobody has touched in months |
| **Interactive surfaces only** | A heartbeat agent touching everything counted as people discovering it |

**What this still cannot tell you.** A capability leaves the block whether or not
the block is why it got used. Shrinkage is equally consistent with the block
working and with it being ignored. That is a counterfactual, and no filter
substitutes for one — see the comparative battery in prd-evals §5.4.

## 6. The discovery block

Installed capabilities minus discovered ones, rendered into the marker-delimited
managed block the instruction-file write-back already maintains across thirteen
harness rules files.

- **Task-shaped, not a tool inventory.** Each line leads with the situation and
  names the capability second. An assistant reaches for a capability when it
  recognises the situation; a list of names is skimmed past.
- **Budget-bounded in lines *and* characters.** Real descriptions run to several
  sentences; bounding only lines let one entry reach 300 characters and blow the
  budget sideways.
- **Honest to the install.** An empty capability set renders nothing rather than
  a heading promising what this node cannot do.
- **Derived, never authored.** The trigger comes from the capability's own
  description, framed by its declared side effects. An undeclared side effect is
  framed as a write, matching what the projector already assumes — a discovery
  block that disagreed with the gate about what a capability *does* would be
  worse than silent.
- **Idempotent.** The write-back only writes on change; a block that differed run
  to run would rewrite thirteen files every session for nothing.

### 6.1 Where the block is surfaced (added 2026-09-21)

The block existed and **nothing rendered it**, so the measured half of the loop
worked and the half that changes behaviour did not exist. "People won't use what
they don't know exists. We have to get this in front of them without being
obtrusive."

**The MCP handshake** is the least obtrusive place it can go. Every connecting
client reads the server's `instructions` once at connect:

- No file is created in anybody's repository and nothing is written to disk.
- The platform conventions always come first; discovery is an addition. A block
  that displaced them would trade a real instruction for an advertisement.
- An install where every capability is already in use gets the baseline string
  back **byte-identical** — discovery succeeded, so it stops spending context.
  That is the end state worth having, and it is asserted.
- Never raises. Attaching a client cannot depend on a readable series.

**Declining the telemetry series does not silence discovery.** Wiring the two
together would leave the person who opted out of measurement the least able to
find the platform, which is the opposite of the point. Their block simply never
shrinks.

Verified against the real composed surface, not fixtures: 8 tools in, a 916-char
16-line block out. That check is also how the earlier mapping-registry bug was
found, where unit fixtures returned lists and the real registry returned a
mapping, so every capability was silently dropped.

**Known limitation.** The trigger line is derived from a capability's
description, so a description written as a noun phrase renders as awkward
English ("When someone wants to chart of supported harnesses"). The derivation
stays — an authored trigger per capability is a second contract to keep in sync,
and it would drift. The fix is to write descriptions as verb phrases, which
SKILL.md conformance already asks for, so improving one improves both.

## 7. Metrics

- **Time to first platform call** in a session, and **share of sessions making
  one at all.** Sessions are supplied rather than derived from the series,
  because a session that made no call leaves no record — and those are exactly
  the ones worth counting. Deriving the denominator from the data would silently
  drop every session we most need to see.
- **Reach-for gap**: an assistant using a general-purpose tool for something the
  platform does properly. Each instance is a concrete fix — discovery, naming, or
  a genuinely missing capability.

Baseline, measured and worth keeping visible: an assistant with the platform
configured throughout a long working session made over a hundred tool calls and
reached for it essentially never.

## 8. The measured denominator (added 2026-09-21)

The governance-overhead benchmark states its cost as a percentage of "a typical
tool call (~100 ms)". Nobody measured that 100 ms. The series holds what calls on
this install actually cost, so the benchmark now reads it:

- `observed_latency()` reports `n` always and percentiles only past
  `MIN_LATENCY_SAMPLES` (30). A p95 over four calls is the fourth-slowest call
  wearing a percentile's name, and once printed it gets quoted. Nearest-rank, not
  interpolated — an interpolated percentile invents a latency no call had, which
  is the wrong trade for a series whose point is that the numbers are real.
- `governance_share()` returns **both** ratios, named, because they are not
  interchangeable: the benchmark divides by bare work ("how much longer does
  governance make this take"), while an observed call already contains the
  overhead ("how much of the wait was governance"). Both read as "percent
  overhead" and differ by exactly the overhead.
- Too small a sample yields `None`, not a fallback to the assumed figure. A
  fallback would reproduce the invented denominator wearing the word "observed".
- The bench output now labels its own context block `"denominator": "assumed"`.

The product inherits the weaker of its inputs: the benchmark is one machine, one
process, one run; the observed series is whatever this install happened to do.
Neither is a population estimate, and the report says so.

## 9. Declaration drift, and the check that proposes its own fix (added 2026-09-21)

The discovery block can only ever offer what is **declared**. A capability
registered with `registry.register(name, fn)` carries no description and no
`surfaces`, so it reaches no tool list, no agent loop, no `SKILL.md` and no
block. Measured on this install: **58 of 117 capabilities**, including every
`secrets.*`, `schedule.*` and `release.*` verb.

`axi hygiene stat capabilities` reconciles registered against declared across
every installed extension and emits a paste-ready `SkillSpec` per gap. The same
check is a `Finding` in `audit_node()`, so it rides the `hygiene stat health`
heartbeat — hygiene being, in practice, the one daemon operators consent to run.

Per-capability the report names what declaring it buys: the tool name it would
become, the surfaces it would reach, capability telemetry, ADR-114 authority
rules, and `SKILL.md` generation. Generic doctrine is not acted on; "callable as
`axiom_connector__status` from any MCP client" is.

It never applies anything, never invents a description, and never proposes MCP
for a mutating verb. Where a verb looks read-only but is registered mutating —
the `register()` default, which makes this the common case — it raises a hint
naming the one-word change, as a question rather than a correction.

A ratchet (`MAX_UNDISCOVERABLE` in `tests/infra/test_extension_surface_sweep.py`)
holds the floor and fails loudly when the count falls, so a gain gets locked in
rather than drifting back.

See ADR-122.

## 10. Measuring overhead against other frameworks (added 2026-09-22)

`axiom.evals.overhead` turns the governance-overhead benchmark into something
another framework can be run through. "8 ms of governance" is unfalsifiable on
its own; it means something only beside what alternatives charge for the same
guarantees, and the honest possibility is that we are expensive.

A **harness** contributes named `Variant`s and the engine does timing,
allocation, percentiles and deltas. `axiom.evals.adapters.axiom_governance`
is this platform; `…adapters.null` is the ungoverned floor. Third-party
adapters load by dotted path, so measuring a competitor never adds a dependency.

Three properties carry the comparison:

- **One shared action body** (`overhead.action_body`). If each harness supplied
  its own, the benchmark would compare the bodies. This is why the adapter
  constructs its seams rather than reusing the standalone bench, and why its
  absolute numbers are not continuous with that bench's published run.
- **Every variant declares the guarantees it buys**, and `compare()` refuses to
  rank a row whose harnesses declare different ones. Measured live: the
  ungoverned harness costs 0 µs and buys nothing, ours costs ~14 ms and buys ten
  guarantees; the row is reported `comparable: false` rather than letting the
  framework offering least win.
- **A canonical name for the composed path** (`COMPOSED_VARIANT`,
  `"governed_full"`). Harnesses are only comparable where variant names line up.
  Without an agreed name every adapter invents its own and the comparison is
  empty by construction — which it silently was, until a real run produced a
  report with nothing in it. `compare()` now says so instead of returning
  emptiness that reads like "no difference found".

Percentiles are nearest-rank; an interpolated percentile invents a latency no
iteration had.
