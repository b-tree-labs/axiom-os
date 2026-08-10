<!-- Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs.
     SPDX-License-Identifier: Apache-2.0 -->

# ADR-099 — Harness shim contract: coexistence with harness-native features, consent-gated write-back

**Status:** Proposed
**Date:** 2026-07-20
**Relates to:** ADR-087 (cross-mem), ADR-092 (sync + write-back),
ADR-096 (guest vs host), ADR-098 (session plane)

---

## Context

Harness makers are building the same features we federate: native
memory, cloud session sync, skills/instruction ecosystems. Two concerns
follow. First, **collision**: if we write into stores their runtimes
also mutate, we can corrupt their state, break on their schema changes,
double-sync against their cloud, or create memory echo loops. Second,
**write-back at user discretion**: users legitimately want Axiom to
*update* a harness's memory files, agent skills, and configuration —
but only when they say so, and reversibly.

Today's touches each solve a slice — read-only absorb adapters,
structure-preserving config registration (tomlkit for TOML, additive
JSON), restore-only-when-absent bundle restore, the ADR-092 write-back
engine — but no single contract governs a harness integration, and each
new harness re-decides these questions implicitly.

## Decision

Every harness gets exactly one **shim** — the adapter that owns *all*
touches of that harness — bound by five rules:

### D1 — One shim, declared surfaces

A shim declares every surface it touches, each tagged with a mode:
`read` (absorb source), `write-extension-point` (documented
user-config surfaces: MCP config, instruction files, skills dirs),
`restore-only` (bundle restore into absent paths), or `never`
(credential/auth stores, vendor-internal databases the runtime
mutates). The declaration is the review artifact: adding a surface is
an ADR-visible change, not a code detail.

### D2 — Guest doctrine (collision avoidance)

We are a guest in the harness's home:

- **Vendor-runtime-owned state is read-only.** Their databases and live
  transcript logs are absorbed, never written. Restore writes only
  files that do not exist — the running harness always wins.
- **Writes go only to documented extension points** — the surfaces the
  vendor built *for* users and third parties (MCP registration,
  instruction files, skills directories) — plus our own namespaced
  files. Nothing else, regardless of how useful it would be.
- **Structure-preserving editors only.** Config edits round-trip
  comments and ordering (tomlkit for TOML; key-additive JSON). A config
  write that would reformat a user's file is a defect.
- **Vendor sync deference.** When a harness's own cloud sync owns a
  store, the shim declares it; transcript restore into that store is
  opt-in (the user chooses which sync is authoritative there), so we
  never race a vendor's replication.
- **Version fingerprints.** A shim pins the store layouts it was tested
  against; an unrecognized layout degrades to skip-with-named-reason,
  never guess-and-write.

### D3 — Sentinels and three-way merge for shared files

Anything we author into a file the user or vendor also edits
(instruction files, skills) sits inside managed begin/end sentinel
blocks, with the last-applied content recorded on our side. Updates are
three-way merges: last-applied vs. current vs. desired — edits by the
user or the vendor win, conflicts queue for review, and uninstall
removes exactly our blocks and nothing else.

### D4 — Consent ladder (write-back at the user's discretion)

Per shim, per surface, the user sets: `off`, `propose` (default —
show the diff, apply on approval, back off after refusals per the
standing RACI escalation), or `auto` (for surfaces the user has
promoted). `axi memory port` remains **additive-only** — onboarding
never rewrites what a harness already has. Applying updates to a
harness's memory files, skills, and config is a distinct verb that
always offers a dry-run diff first.

### Slash commands are a declared surface too

When one harness fronts another (a native UI driving a different
harness's engine through the gateway), the **front-of-glass harness
owns the slash prefix**: it intercepts its own commands before the
model sees them, and unknown slashes pass through as plain text — which
the backend harness may then interpret as *its* commands or skills.
Interactive-only commands are meaningless to a headless backend. A shim
that fronts or backs another harness declares this command surface
(intercepted / passed-through / dead) so users aren't left guessing
which layer ate their slash.

### D5 — Echo and duplicate safety against vendor memory features

Absorbed content carries its `SourceOrigin`; write-back stamps
idempotency keys — so absorb-of-our-own-write-back is echo-suppressed
and loops cannot form. A shim additionally declares which native memory
features the harness has, so dedup treats vendor-remembered content as
a same-source coordinate rather than a fresh fact.

## Consequences

- Collision risk becomes a reviewable property: a shim's surface table
  says exactly what could conflict with a vendor's roadmap, and D2
  confines us to surfaces vendors are committed to keeping stable
  (their own extension points).
- Vendor updates degrade us gracefully (fingerprint → skip + warn)
  instead of corrupting either side.
- Write-back becomes a product surface with an explicit trust dial,
  not a side effect — and uninstall is clean, which is the difference
  between a guest and an infestation.
- Cost: shims carry declarations and version pins that need upkeep as
  vendors ship. That upkeep is the price of touching other people's
  ecosystems; the alternative (implicit behavior) pays it in
  incidents instead.

## Field notes (2026-07-20 live testing)

Same-day evidence for the doctrine above, from wiring one vendor's
native UI to a headless-harness backend through the gateway:

- **Drift is real and fast.** Two config-convention breaks were hit in
  one session: profile tables moved out of the main config file into
  per-profile files, and the chat-completions wire protocol was dropped
  entirely (Responses-only). Both were caught safely because writes
  were additive and structure-preserving (D2); both are exactly what
  version fingerprints (D2) exist for.
- **Proprietary sidecar endpoints exist.** The vendor's client calls a
  models-manifest endpoint with a proprietary schema alongside the
  standard one; serving only the standard shape degrades it. Shims
  fronting a serving face must declare which sidecar endpoints the
  client needs.
- **Agentic clients are latency-brittle against headless backends.** A
  cold headless-harness launch (tens of seconds to first token under a
  large agent prompt) sits far outside the streaming cadence agent
  loops expect; retry logic then multiplies abandoned backend runs.
  Chat-shaped clients are fine. Incremental streaming + session
  pinning is the known mitigation; until then a shim declares the
  backend as chat-grade, not agent-grade.

## Open questions

- Verb naming for consent-gated updates (`axi memory project`? a
  harness-scoped noun?) — pick when the first write-back surface
  beyond instruction files ships.
- Whether shim declarations live in code (typed tables, as absorb
  specs do today) or in the extension manifest for external
  discoverability.
- Sentinel format standardization across file types (markdown comments
  vs. TOML/JSON keys).
