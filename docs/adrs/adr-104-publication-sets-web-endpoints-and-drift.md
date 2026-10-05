# ADR-104 — Publication sets, harness-agnostic artifacts, and destination drift

**Status:** Draft — 2026-08-24
**Owner:** @ben
**Related:** [spec-publisher.md](../specs/spec-publisher.md) (§1.3 state and registry, §1.4 engine workflow, §1.6 multi-destination failure handling, §3.3 `StorageProvider` ABC, §6 endpoint catalog, §7 audience system), ADR-101 (PRESS generation provider precedent), ADR-059 (connector-first providers), ADR-012 (provider identity), ADR-063 (generated artifacts). Supersedes nothing.

## Context

PRESS already models multi-destination publication well. `PublisherEngine` fans one
generated artifact out to N storage providers (§1.4 step 11), collects per-destination
results independently, governs failure with a `required` flag (§1.6), records
`artifact SHA256` and per-destination URLs in state, maintains a `doc_id → published_url`
registry, gates endpoints by declared audience (§7.6), and can watch source directories
to publish on save. The endpoint catalog (§6) already declares `supports_pull` for
"the publisher agent's drift detection loop." §7.6's own worked example rejects a
`github-pages` endpoint for a `restricted` document — an endpoint the spec anticipates
but which does not exist.

Meanwhile, every frontier assistant now produces **rendered artifacts** — a self-contained
HTML page, a canvas, a report — and every vendor hosts them differently. Those surfaces
are convenient for authoring and review, and they are not publication:

- They are **account-scoped**. An unauthenticated fetch of a hosted artifact returns a
  generic shell with none of the content, so a link shared outside the vendor's tenancy
  silently shows nothing rather than failing loudly.
- They are **deletable out from under you**. We have observed three published pages become
  unreachable with no notice and no local copy.
- Their **update semantics are vendor-specific**. On at least one surface the URL *is* the
  update handle: re-publishing without it forks a second copy rather than updating.
- They are **not portable**. Nothing about them survives a change of assistant.

Four gaps follow.

**1. There is no vendor-neutral web-publication endpoint.** The catalog covers filesystem,
object storage, Microsoft and Google targets. Teams publishing rendered pages have no
first-class endpoint, so those pages are produced *outside* the pipeline and inherit none
of its state, audience gating, content gate, or provenance.

**2. Artifacts produced outside the pipeline have no durable source.** The working file
typically sits in a scratch directory, which is cleared. Recovery of the incident above
was possible only because a transcript happened to retain the tool calls that produced the
file — replay, not design.

**3. State models the source moving ahead of the destination, but not the reverse.**
Status is `draft | published | stale`. That detects *source-ahead*. It cannot detect
*destination-ahead*: a surface editable outside the pipeline can be changed by a person, or
re-published by another agent session, while state still reads `published`.

**4. `doc_id` identifies a single file**, so a primary document and its companions —
presenter notes, a technical annex — share no identity. Losing one is invisible from the
others. In the incident above one member of a three-part set was recovered and the other
two were not even enumerable.

## Decision

### D1 · The producer contract is a file on disk, and nothing else

Any assistant, in any harness, participates by writing **a complete, self-contained file**
into a watched directory. No SDK, no vendor API, no plugin. `axi pub watch` already
watches `source_dirs`; this makes the loop harness-agnostic by construction — it works
identically for an agent with a Write tool, a human with an editor, or a canvas exported
by hand.

A publishable rendered artifact MUST be:

- **A complete document.** Not a fragment. Vendor viewers commonly supply the surrounding
  `<head>`, which hides the fact that the file is not viable alone. A file that renders
  only inside its producer's viewer is not publishable.
- **Self-contained.** No external fetches — styles, scripts, fonts, and images inlined or
  embedded. This is what makes it archivable, printable, and viewable offline, and it is
  what makes byte-level recovery meaningful.
- **Explicit about encoding.** Declaring its own charset, for the same reason.

These are testable properties, and the engine SHOULD verify them at push time rather than
trusting the producer.

### D2 · A publication set is declared by one manifest

```yaml
# quarterly-review.pub.yaml
doc_id: quarterly-review
audience:
  org_scope: partner
  access_tier: internal
endpoints: [gh-pages]           # authoritative — the publication
preview:   [claude-artifact]    # optional, non-authoritative (see D3)
members:
  - id: deck
    file: deck.html
    primary: true
  - id: notes
    file: presenter-notes.html
  - id: annex
    file: technical-annex.html
```

One file declares identity, audience, destinations and membership. It is the sidecar for
formats that cannot carry YAML front matter (HTML, PDF), and it subsumes front matter for
those that can. It lives in the repository beside its members, so the set is durable and
enumerable by construction — which is precisely what was missing when three related pages
were lost.

`doc_id` therefore addresses a **set**; state and registry key on
`(doc_id, member_id, endpoint)`. A single-member set is the common case and behaves
exactly as today.

### D3 · Authoritative endpoints and preview surfaces are different things

This is the load-bearing distinction, and it is what keeps the design vendor-neutral.

|  | **Authoritative endpoint** | **Preview surface** |
|---|---|---|
| Examples | `gh-pages`, `local`, object storage, any static HTTP mount | vendor artifact/canvas hosting |
| Source of truth | never — the repository is | never |
| May vanish without notice | no | **yes, expected** |
| In the registry | yes | recorded, but advisory |
| Counts toward `required` success | yes | **never** |
| Link-integrity checked | yes | no |
| Audience-gated | yes (§7.6) | yes, and additionally by vendor tenancy |

A preview surface is a **cache of a publication, not a publication**. It is useful — fast
review, in-loop authoring — and the design must never let it become load-bearing. Its
absence is not a failure; its deletion is not an incident.

Concretely: `PublisherFactory.register("storage", "gh-pages", GitHubPagesStorageProvider)`
registers an authoritative endpoint. Vendor surfaces register in the same category but
carry `authoritative: false` in their catalog entry, and the engine excludes them from
`required` accounting and from `check-links`.

### D4 · Update handles belong to preview surfaces, not to publications

An authoritative endpoint is **content-addressed**: the repository commit is the handle,
and republishing the same source is idempotent because the content determines the result.
No extra state is needed.

A preview surface may require an opaque **update handle** — on at least one vendor the
destination URL is that handle, and pushing without it creates a second copy rather than
updating the first. Where a provider declares `requires_update_handle`, the registry MUST
persist the handle returned by first publication, and the engine MUST supply it on every
subsequent push to that surface. A provider that declares the requirement and finds no
recorded handle MUST treat the push as a first publication and persist the result before
reporting success.

Confining this to preview surfaces keeps the fragile, vendor-specific state out of the
publication path entirely.

### D5 · New lifecycle state: `drifted`

Status becomes `draft | published | stale | drifted | superseded | retired`.

| State | Source vs destination | Meaning |
|---|---|---|
| `draft` | not published | in the workspace, no destination yet |
| `published` | hashes agree | destination reflects the recorded source |
| `stale` | source ahead | source changed since last push — push to reconcile |
| `drifted` | destination ahead | destination changed outside the pipeline |
| `superseded` | — | replaced by another set; retained and marked, not deleted |
| `retired` | — | no longer served; destination serves a pointer to its successor |

Detected by the publisher agent's pull loop comparing the pulled destination hash against
the recorded `artifact SHA256`. Both `gh-pages` and vendor preview surfaces declare
`supports_pull: true`, since both can be read back over HTTP.

`drifted` is **not** an error and MUST NOT be auto-reconciled — a destination edited by a
person is a signal, not corruption. Both-sides-changed reports `drifted` with a
`source_also_changed` flag and requires an explicit operator verb
(`axi pub reconcile <doc_id> --take source|destination`) so the losing side is always a
stated choice.

### D6 · Retirement serves a pointer; deletion does not

`retired` is terminal and **serves**. Where the endpoint can express it, a retired page
responds with HTTP 410 Gone and a body naming its successor; where it cannot — a static
host generally cannot 410 a path it still serves — the page is replaced by a stub that
links onward. Registry entries for retired members are retained so inbound links resolve
to an explanation rather than a 404.

This generalises the platform's existing retired-endpoint convention, where a sunset
service keeps answering with the identity of its replacement. A 404 discards the single
piece of information the reader needs.

## Behavior

- **Set push.** `axi pub push <doc_id>` generates and pushes every member. Per-member,
  per-destination results are collected; `required` semantics (§1.6) apply per member and
  ignore preview surfaces entirely.
- **Producer loop.** An assistant writes files plus a manifest into a watched directory;
  `axi pub watch` publishes on save. Identical for every harness.
- **Drift check.** The publishing agent's tick pulls each `published` member from each
  `supports_pull` endpoint and transitions to `drifted` on mismatch. Cadence is
  endpoint-configurable; pull failure degrades to a warning and never mutates state.
- **Audience gating is unchanged.** `check_endpoint_policy()` (§1.4 step 4) still runs
  before any generation work — §7.6's worked example rejecting `github-pages` for a
  `restricted` tier becomes executable rather than illustrative.
- **No new publishing path.** Nothing bypasses generation, the content gate, provenance
  pre-flight, or notification. A rendered page is an artifact like any other.

## Consequences

- **+** Any frontier assistant can publish through one vendor-neutral path by writing a
  file. No vendor SDK enters the provider layer, and changing assistants changes nothing
  downstream.
- **+** Source of truth moves into the repository. The failure that motivated this ADR —
  destination lost, source lost, recovery only by transcript replay — stops being possible
  for anything published through the pipeline.
- **+** The authoritative/preview split lets vendor surfaces stay useful for authoring
  without ever becoming load-bearing. Their deletion becomes a non-event.
- **+** Sets make companions enumerable, recoverable and retirable together, which is how
  they are authored and read.
- **+** `drifted` closes a real blind spot: today a destination edited outside the pipeline
  leaves state confidently wrong, with nothing that would ever notice.
- **−** The self-contained requirement (D1) is a real constraint on producers — no CDN
  fonts, no external scripts. It is also what makes archival and offline viewing work, so
  it is a deliberate trade rather than an incidental cost.
- **−** Pull adds recurring network work per published member per endpoint. Mitigated by
  configurable cadence and non-fatal failure.
- **−** Set semantics touch registry and state schemas, PostgreSQL-backed in multi-agent
  mode (§1.3). Migration is required for `publisher_registry` and `publisher_documents`.
- **−** Web publication makes audience misconfiguration more consequential than a
  misdirected file upload. The catalog therefore separates push credentials from viewer
  scope, so the gate can reason about the second.

## Open questions

1. **Pull fidelity.** A destination may transform what it serves — wrapping, minification,
   injected chrome — so hashing the served bytes will false-positive. Prefer providers
   publishing a content digest alongside the artifact and comparing that; fall back to a
   normalised extraction of the document body where they cannot.
2. **Manifest discovery.** Whether `*.pub.yaml` is discovered by convention within
   `source_dirs`, or must be registered via `axi pub onboard`. Convention is friendlier to
   an assistant that just wrote a file; registration is friendlier to review.
3. **Retirement without 410.** Whether a stub page satisfies `axi pub check-links`, or
   whether link integrity needs a distinct `retired` verdict separate from `broken`.
4. **Verifying D1 mechanically.** "Complete and self-contained" is testable — parse for
   external references, assert a charset, assert a document root. Whether that belongs in
   the content gate or as a pre-flight check of its own is unresolved.
