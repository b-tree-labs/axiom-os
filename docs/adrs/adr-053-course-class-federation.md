# ADR-034 — Course (canonical template) and Class (scheduled instance) in the federated world

**Status:** Proposed (sketch — refine before Prague summer 2026)
**Date:** 2026-04-30
**Drivers:** Benjamin Booth (product lead), a domain researcher (co-lead)
**Decision target:** 2026-05-15 (Prague Phase 0.1 deadline)

## Context

Keplo (the classroom extension) currently treats each classroom as a
self-contained instantiation: instructor runs `axi classroom prep init`,
uploads materials, configures RAG/prompts/assessments, and runs a
single classroom server. This works for one cohort at one institution.

It does **not** work for the federated world we're building toward,
where:

- Multiple institutions run instructionally-equivalent cohorts
  (partner universities and labs) — they need *shared* methodology,
  evaluation, and curriculum.
- Curriculum updates from one author should propagate (with consent)
  to downstream institutions running derivative cohorts.
- Pedagogical claims ("this module passes mastery at 92% across N
  institutions") need to be cryptographically attestable.
- Transfer students should carry consent-based learning context
  between institutions.

We need an architectural separation between:
- The **canonical, replicable, forkable thing** (a curriculum design)
- The **running, time-bound, institutionally-anchored thing** (a
  cohort)

## Decision

### Course — canonical template, federation-citable artifact

A **Course** is a content-addressable manifest that fully describes
a curriculum design:

- Identity: `axiom://course/<authoring-org>/<slug>/<version>`
  *(e.g., `axiom://course/example-org/course-101/v1.0.0`)*
- Versioned (semver: `v1.0.0`, `v1.1.0`, `v2.0.0`)
- Multi-authority signed (original author + each fork along the chain)
- Stored in a **federation Course Registry**
- Replicable across the federation; forkable by any peer institution
  (with attribution chain preserved)
- Domain-agnostic schema; supports any subject area

The Course manifest declares:
- Title, description, learning objectives
- Module sequence (week-by-week or unit-by-unit)
- Default RAG policy (course_only / course_plus_inst / etc.)
- Default LLM tier per learning mode (tutor=standard, quiz=dumb, etc.)
- Default prompt scaffolds (tutor / quiz / reflect / review)
- Materials manifest (or pointer thereto) — content-addressable
  fixtures
- Assessment bank (with rubrics)
- Federation metadata: authoring org, attribution chain, signed
  manifest hash

Courses are **immutable per version**. Updates produce new versions;
forks produce new Courses with attribution back to source.

### Class — scheduled instance, institutionally anchored

A **Class** is a *running cohort* that instantiates a Course at a
specific institution, time, and roster:

- Identity: `axiom://class/<host-org>/<class-slug>/<term>`
  *(e.g., `axiom://class/example-org/course-101-pilot/2026-summer`)*
- References a Course version it instantiates (`course=axiom://course/example-org/course-101/v1.0.0`)
- Bound to **one** federation peer node (the running instance)
- Has its own:
  - Cohort roster (institutional identity + students)
  - LangFuse project (for sovereign observability)
  - CHALKE state (per-cohort agent context)
  - Materials index (synced from Course manifest + institutional augmentation)
  - Schedule (start date, end date, week-by-week timeline)
  - Local interaction log
- Mutable runtime state (NOT content-addressed)
- Can override Course defaults (institutional customization)
- Can be archived but not deleted (compliance + research evidence)

### Federation patterns enabled

**Forking a Course:**
```
$ axi classroom course fork \
    --from axiom://course/example-org/course-101/v1.0.0 \
    --to axiom://course/partner-org/course-101-partner/v0.1.0 \
    --authoring-org "Partner Institution"
```
Attribution chain preserved; the partner customizes; the source version unchanged.

**Instantiating a Class:**
```
$ axi classroom prep init \
    --from-course axiom://course/example-org/course-101/v1.0.0 \
    --term "2026-summer" \
    --instructor "@user:example-org"
```
Creates Class `axiom://class/example-org/course-101-pilot/2026-summer` running
v1.0.0 of COURSE-101 at the deploying org, the instructor, term summer-2026.

**Course publication / version bump:**
```
$ axi classroom course publish \
    --course-id axiom://course/example-org/course-101 \
    --version v1.1.0
```
Downstream Classes decide whether to rebase to v1.1.0 or pin to v1.0.0.

**Federation discovery:**
```
$ axi classroom course search \
    --tag "engineering" --tag "undergrad" --tag "intro"
```
Returns all federation-published Courses matching tags.

### Storage

- **Course Registry:** federated, content-addressable. Mechanism TBD —
  candidates: federation-built primitive (axiom-native) OR repurpose
  an existing artifact registry (NPM-shaped).
- **Class state:** local to the hosting institution's coordinator
  (current `~/.axi/coordinator/classrooms/<class-id>`). Federation
  metadata + Class identity get announced to the federation but
  internals stay sovereign.

## Consequences

### Positive
- Curriculum becomes a **first-class federation primitive** — citable,
  attributable, forkable, version-tagged.
- KPIs become statistically defensible across multiple Class instances
  of the same Course (lead paper §3 success metrics).
- Privacy is preserved by construction — Class state stays at the
  hosting institution; only Course metadata flows across the federation.
- Accreditation gets a verifiable artifact — "this Course at v1.2.0
  has been instantiated as N Classes producing the following aggregate
  outcomes" with multi-authority signatures.

### Negative
- Adds a new architectural primitive (Course) we have to maintain
  and document. Until Course Registry exists, "Class without a
  Course" remains the only path.
- Forking + version-tracking introduces curriculum-management
  complexity instructors haven't had to think about before.
- Federation Course Registry is a meaningful piece of infrastructure
  to design + build before it's load-bearing.

### Risks
- If Course Registry mechanism choice goes poorly, federation
  curriculum-sharing stalls. Mitigation: pick the simplest viable
  mechanism (signed manifest in git-shaped registry), iterate.
- Course-version compatibility with Class state — when a Course
  changes, do existing Classes auto-migrate or stay pinned? Decision:
  Classes pin until explicit rebase. Course updates do not break
  running Classes.

## Migration path from current state

Currently we have classrooms (Class-shaped) without explicit Course
manifest. Migration:

1. **Pre-pilot (now → 2026-05-15):** Build Course manifest schema +
   the `--from-example` flag in `axi classroom prep init`. Each
   `examples/<domain-name>/` directory becomes a *Course in disguise*.
2. **Pilot launch (2026-06):** the instructor's COURSE-101 cohort runs
   as a Class, but its Course manifest is implicit (the harness
   fixtures + classroom manifest). We commit to formalizing it as
   `axiom://course/example-org/course-101/v1.0.0` post-launch.
3. **Post-pilot (2026-Q4):** Course Registry mechanism chosen +
   built. COURSE-101 Course published. First federation peer (a
   partner institution) forks.
4. **Q1 2027:** Multiple Classes from same Course running; the
   flagship paper "The Federated Tutor" cites this as the empirical
   substrate.

## Open questions

- Course Registry mechanism: federation-native (axiom://) primitive
  or off-the-shelf (git-based, NPM-shaped, IPFS-shaped)?
- How do material *binaries* (PDFs, slide decks, video transcripts)
  travel? Embedded in manifest, or content-addressable side-store?
- What's the migration story for an instructor who wants to sync a
  Class to a NEW Course version mid-term?
- How are Class outcomes (mastery rates, DFW, etc.) attributed back
  to the originating Course for federation-grade pedagogical
  attestation?

## Related

- `axiom/src/axiom/extensions/builtins/classroom/` — Keplo extension
  (where Course/Class CLI lands)
- `axiom/src/axiom/extensions/builtins/classroom/examples/` — example
  Courses (canonical templates serving as the seed registry)
- `axiom/src/axiom/extensions/builtins/classroom/docs/prd.md` — Keplo
  product requirements
- `axiom/src/axiom/extensions/builtins/classroom/docs/papers/pedagogical-intent-rag-eval-draft.md` — lead paper
- ADR-022..025 — federation primitives (cohort registry, A2A,
  multi-authority signatures, trust graph)
