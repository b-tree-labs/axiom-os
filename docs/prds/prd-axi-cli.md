# Product Requirements Document: axiom CLI

> **Implementation Status: 🟡 Partial** — Core CLI, extension discovery, and ~30 builtin extensions shipped. Agents (AXI, SCAN, TIDY, PRESS, TRIAGE, RIVET, CURIO) operational. Cross-harness command generation shipped (#113). Tier-aware revelation, smart help, hot-load protocol, CLI↔chat parity, and shell-completion lifecycle are designed (this PRD) but not yet implemented; see [§Status & Phasing](#status--phasing).

**Module:** axiom Command-Line Interface
**Status:** Active Development
**Last Updated:** 2026-05-02
**Parent:** [Executive PRD](prd-executive.md)
**Tech Specs:** [axiom CLI Specification](../specs/spec-axi-cli.md), [Extension Loading](../specs/spec-extension-loading.md)
**Sub-PRDs:** [Cross-harness Commands Generator](prd-commands-generator.md)

---

## Executive Summary

`axi` is the unified command-line interface for Axiom. It provides operators, researchers, and developers a single entry point for interacting with the platform — from querying ops logs to managing surrogate models to orchestrating simulations.

The CLI embodies the platform philosophy: **power tools for experts, sensible defaults for everyone else**.

Every capability in Axiom ships as an **extension**, discovered via `axiom-extension.toml` manifests. The CLI is the primary interface for both humans and agents — the same commands a user types are the same commands agents invoke programmatically.

---

## Status & Phasing

This PRD has accumulated several capabilities that span "already shipped"
through "strategic, post-launch evolution." This section is the single
place that answers "is this feature live, in flight, or aspirational?"
and groups the work into phases for scheduling.

### Legend

| Badge | Meaning |
|---|---|
| ✅ **Shipped** | In `main`, available to users today |
| 🟡 **Partial** | Foundation merged; one or more dimensions still pending |
| 🟦 **Designed** | Spec'd in this PRD or a sibling doc; implementation not started |
| 🔵 **Strategic** | Direction agreed; deliberately deferred to a future phase |

### Capability rollup

| Capability | Status | Phase | Source of truth |
|---|---|---|---|
| Core CLI dispatcher (`axi <noun> <verb>`) | ✅ Shipped | — | `axiom_cli.py` |
| Builtin extensions (~30) | ✅ Shipped | — | `src/axiom/extensions/builtins/` |
| Extension discovery from AEOS manifests | ✅ Shipped | — | `extensions/discovery.py` |
| `axi chat` REPL (Ask / Plan / Agent modes) | ✅ Shipped | — | `extensions/builtins/chat/` |
| In-chat slash commands (40+) | ✅ Shipped | — | `chat/commands.py` |
| Cross-harness shim generator (`axi commands`) | ✅ Shipped | — | [#113 / `prd-commands-generator.md`](prd-commands-generator.md) |
| RIVET lifecycle agent + cloud-routine watcher | ✅ Shipped | — | #107 |
| REV-U Phase 1 PR review agent | ✅ Shipped | — | #108 |
| TIDY stale-worktree skill | ✅ Shipped | — | #109 |
| Capability tiers in AEOS manifest schema | ✅ Shipped (schema) / 🟡 Partial (consumers) | Phase 1 | `aeos-manifest-0.1.json`; consumers in this PRD |
| Sweep existing manifests with tier + intent_groups | 🟦 Designed | Phase 1 | This PRD §Progressive Disclosure |
| Reveal mechanism (`--all`, tab-twice, chat surfacing, `--pin`, `competency set`) | 🟦 Designed | Phase 1 | This PRD §Progressive Disclosure → Reveal |
| AEOS verb-grammar lint + audit migration sweep | 🟦 Designed | Phase 1 | `spec-aeos-0.1.md §4.3.1`, `docs/working/cli-verb-grammar-audit-2026-05-02.md` |
| Identity sharpening: AXI welcome banner + per-turn speaker prefix | 🟦 Designed | Phase 1 | This PRD §The Agent Team, §Agent Addressing |
| Retire "Neut" overlay across docs | ✅ Shipped (this PR) | — | This PRD §The Agent Team, §AXI: The Orchestrator |
| Agent direct-addressing in chat (`@axi`, `@tidy`, …) | 🟦 Designed | Phase 1 | This PRD §Agent Addressing |
| `@`-handle tab-completion (federation-aware, three sublist modes) | 🟦 Designed | Phase 2 | This PRD §Agent Addressing → Tab-completion |
| Federated agent addressing (`@<agent>:<server>`) | 🟦 Designed | Phase 2 | This PRD §Agent Addressing |
| Native peer-to-peer chat (human ↔ human, with optional agents in the room) | 🔵 Strategic | Phase 3 | This PRD §Looking ahead → `spec-peer-to-peer-chat.md` (TBD) |
| `axi help` smart, tier-aware, dynamic | 🟦 Designed | Phase 1 | This PRD §Progressive Disclosure |
| Per-extension familiarity tracking | 🟦 Designed | Phase 1 | `spec-axi-cli.md §Capability Tiers` |
| Hands-free graduation between tiers | 🟦 Designed | Phase 1 | `spec-axi-cli.md §Capability Tiers` |
| Intent groups (`axi help <group>`) | 🟦 Designed | Phase 1 | This PRD §Progressive Disclosure |
| Generator tier-filtering (default `--tier starter`) | 🟦 Designed | Phase 1 | `prd-commands-generator.md §5` |
| CLI history with provider abstraction | 🟦 Designed | Phase 2 | `spec-axi-cli.md §CLI History and Decay` |
| Memory-backed history (Axiom Memory episodic) | 🟦 Designed | Phase 2 | `spec-axi-cli.md §CLI History and Decay` |
| Adaptive decay curve | 🔵 Strategic | Phase 3 | `spec-axi-cli.md §CLI History and Decay` |
| Chat ↔ CLI parity (resolution path, hand-off ingest) | 🟦 Designed | Phase 2 | `spec-axi-cli.md §Chat ↔ CLI Resolution` |
| Cross-harness session mirroring — `axi chat` ↔ canonical transcript store | 🟦 Designed | Phase 2 | This PRD §Cross-harness session mirroring |
| Cross-harness session mirroring — Claude Code / Cursor / Codex / OpenCode adapters | 🔵 Strategic | Phase 3 | This PRD §Cross-harness session mirroring → `spec-cross-harness-mirroring.md` (TBD) |
| Hot-load + hot-swap of extensions (Python loader) | 🟦 Designed | Phase 2 | `spec-extension-loading.md §2-§5` |
| Reliable shell auto-complete in every install mode | 🟡 Partial (argcomplete today) / 🟦 Designed (lifecycle verb + tier-aware) | Phase 2 | This PRD §Reliable shell auto-complete |
| `axi completions {install, print, refresh, uninstall}` verb | 🟦 Designed | Phase 2 | `spec-axi-cli.md §Shell Completions` |
| WASM-backed extension loader | 🔵 Strategic | Phase 3 | `spec-extension-loading.md §6` |
| ADR for WASM loader | 🔵 Strategic | Phase 3 | `adr-NNN-wasm-extension-loader.md` (TBD) |
| Future domain commands (`log`, `model`, `twin`, `data`, `infra`) | 🟦 Designed | Phase 3 | This PRD §Planned Commands |

### Phase windows

- **Phase 1 — Prague-blocking (target: by 2026-06-08 class start).**
  Tier model and smart help so the CLI's growing surface stays usable
  for novice instructors at Prague. Implementation: AEOS schema sweep,
  `axi help` rewrite, generator `--tier` flag, per-extension familiarity
  tracking. Bound by an existing 5-week runway.
- **Phase 2 — Quality + coverage (target: summer 2026, post-Prague).**
  Hot-load protocol so installing extensions is felt immediately;
  CLI ↔ chat parity so users can hand off mid-stream; auto-complete
  lifecycle verb; CLI history with memory-provider abstraction.
- **Phase 3 — Strategic foundation (target: fall 2026 onward).**
  WASM extension loader (own ADR); adaptive decay learning;
  domain-command rollouts (`log`, `model`, `twin`, `data`, `infra`).

The "Phase 3 — Strategic foundation" items are **not** committed delivery
windows; they are direction. Each will earn a dedicated execution slot
when foundations under it are stable.

---

## Problem Statement

Domain teams currently interact with their data, agents, and
computational tools through:

- **Fragmented interfaces**: web dashboards, ad-hoc scripts, manual
  file transfers, separate vendor portals.
- **No automation path**: can't script common queries, deployments, or
  multi-step workflows reproducibly.
- **Platform lock-in**: GUI-only tools prevent integration with CI/CD,
  source control, and other agentic harnesses.
- **Tribal knowledge**: "how to run X" lives in people's heads, not
  in reproducible, shareable commands.
- **Sovereignty + cross-org coordination tension**: organizations need
  to keep data inside their authority while still collaborating across
  peer cohorts. Off-the-shelf platforms typically force a choice
  between hosted convenience (cede sovereignty) or on-prem isolation
  (lose collaboration). Axiom's federation primitives are designed to
  refuse that tradeoff.

### Where Axiom is uniquely positioned

The shared theme across Axiom's target domains is an **industrial or
commercial need for strong security paired with federation across
cohorts**. The platform addresses (each shipped as its own extension,
not baked into Axiom's core):

- **Research labs** — federated knowledge contribution across
  institutions, agent-augmented literature + code synthesis, durable
  research provenance
- **Classrooms** — cohort-based learning with cross-institution
  curriculum sharing and per-student federated memory (Keplo)
- **Factories and plant facilities** — power, water, utilities,
  process-industry sites with strict data-sovereignty requirements
  and shift-handoff workflows
- **Field and military deployments** — disconnected-tolerant
  operation, classification-aware routing, attested cross-unit
  collaboration
- **Company organizations** — multi-team coordination with org-
  internal RAG, cross-team knowledge sharing under role-scoped
  visibility
- **DevOps + SRE teams** — pipelines, runbooks, incident response,
  release coordination across services
- **Content publishing pipelines** — authored material flowing
  through review, approval, distribution with federated
  contributor cohorts

Domain-specific vocabulary lives in those extensions; this PRD
governs the core, domain-agnostic CLI surface they all build on.
When other sections in this PRD give domain *examples*, they pick
2–3 representative ones from this list that fit the local context
— the exhaustive list lives here.

---

## User Personas

The personas below name the **human roles** Axiom is uniquely suited
for. We deliberately exclude roles that the shipping agent team
already absorbs — DevOps work largely belongs to RIVET (releases) +
TIDY (hygiene) + TRIAGE (diagnostics); data-pipeline drudgery belongs
to RIVET + extension hooks; technical-writing-as-process belongs to
PRESS; junior research-assistant work belongs to CURIO; PR review
belongs to REV-U. Listing those as personas would conflate "who uses
the platform" with "what the platform automates."

What's left are the roles that bear **accountability and intent** —
the humans whose curiosity, teaching, learning, judgment, or
governance Axiom is built to amplify. Each role typically operates
within one or more *domain extensions* (Keplo for classrooms,
a domain consumer for scientific-workload operations, future extensions for
factories, field deployments, company orgs, and other domains
named in [§Problem Statement → Where Axiom is uniquely positioned](#where-axiom-is-uniquely-positioned));
the platform is what they share.

| Persona | Why Axiom is uniquely suited | Typical CLI surface | Technical level |
|---|---|---|---|
| **Student / Learner** | Federated classroom membership, agent-augmented tutoring (CHALKE), durable cross-course memory, peer cohorts | `axi chat`, `axi classroom take`, `axi memory show` | Basic |
| **Instructor / Educator** | Curriculum authoring with community RAG, per-student progress visibility, peer-instructor knowledge sharing across cohorts | `axi classroom prep`, `axi classroom serve`, `axi classroom proposals`, `axi pub` | Intermediate |
| **Researcher / Investigator** | Federated knowledge contribution, agent-augmented literature + code synthesis (CURIO), cross-lab cohort collaboration, durable research provenance | `axi research`, `axi rag`, `axi memory show`, `axi pub` | Intermediate to Advanced |
| **Domain Operator / Practitioner** | CLI surface to domain-specific workflows shipped as extensions — plant control rooms, lab procedures, fielded military operations, factory floors, content pipelines | `axi <domain-noun> <verb>` (varies per installed domain extension) | Basic to Intermediate |
| **Extension Author / Builder** | AEOS framework, agent team available as collaborators (REV-U for review, RIVET for releases), cross-harness reach via the commands generator | `axi ext init`, `axi ext lint`, `axi review`, `axi commands generate` | Advanced |
| **Federation Steward** | Node and cohort management, trust-graph operation (ADR-028), WARDEN-mediated cross-org peering, signed-extension distribution | `axi federation`, `axi nodes`, `axi directive`, `axi security` | Advanced to Expert |

The "Technical level" column governs the **default surfacing** the
user encounters under the [§Progressive Disclosure](#progressive-disclosure) rules — a Student
starts at the `starter` tier and graduates per-extension as they
explore; a Federation Steward tends to operate at `advanced` from
day one because their job description is the platform itself. This
is the *expectation* baseline; per-user familiarity tracking
overrides it as competency accumulates.

---

## User Journeys (per persona)

The same six-stage shape (install → first task → discovery + graduation
→ new-extension hot-load → mid-stream hand-off → power use) plays out
differently for each persona above. Three full walkthroughs follow for
the personas with the most-distinct paths through the platform
(Student, Instructor, Extension Author); three sketches cover the
rest. Each hits the same mechanisms but with persona-appropriate
verbs, vocabulary, and graduation cadences.

### Common shape

> **Day 1 — Install + first-run** → tab-completion auto-wired (per
> [§Reliable shell auto-complete](#reliable-shell-auto-complete-every-install-mode));
> `axi help` shows only the `starter` tier (~10–15 verbs).
>
> **Day 1 — First task** → user runs the persona-canonical verb;
> chat session ingests no history (none yet) and opens fresh.
>
> **Week 1 — Discovery + graduation** → CLI history accumulates per
> [`spec-axi-cli.md §CLI History and Decay`](../specs/spec-axi-cli.md#cli-history-and-decay);
> per-extension competency thresholds clear silently
> (per [§Progressive Disclosure → Hands-free graduation](#progressive-disclosure)).
>
> **Week 2 — New extension** → `axi ext install <name>` is felt
> immediately (per [§Hot-load and hot-swap](#hot-load-and-hot-swap-of-extensions));
> the new extension's verbs surface at the user's per-extension
> familiarity for *that* extension, not their global tier — even
> seniors enter foreign surfaces as novices.
>
> **Month 2 — Hand-off** → `axi chat` ingests recent CLI history; the
> user picks up mid-stream without re-explaining (per
> [§CLI ↔ Chat Parity](#cli--chat-parity)).
>
> **Year 1 — Power use** → user has graduated into `advanced` for
> their working surface; chat proposes the resolved CLI invocation
> before executing every action; the parity invariant has become
> invisible (which is the point).

### Journey: Student

> A student installs the cohort's domain distribution
> on their laptop a week before class.

```bash
$ pip install <distribution>                      # ships axi + Keplo + domain extensions
$ axi
  Welcome. I detected zsh. Want me to set up tab-completion now? [Y/n]
  ✓ Completion installed at ~/.zsh/completions/_axi

$ axi chat
  AXI here. (axi v0.10 · sonnet via gateway · cwd ~/ne-101)
  I don't see any recent activity yet. What would you like to start
  with — exploring the course materials, joining the cohort, or
  something else?
```

**Day 1 first task** is conversation, not a verb. AXI discovers
the student's intent and runs `axi classroom join --cohort=sp26-cze-prague`
behind a confirmation prompt (per the resolution-preview pattern
P2 — "I'll run: …").

**Week 1 — discovery + graduation.** The student runs `axi chat` daily
to ask coursework questions. Their familiarity with `chat` clears the
threshold; `core`-tier verbs in the chat extension (e.g.
`/save`, `/sessions`, `/permissions`) start appearing in
the slash palette. They've never typed an `axi`-noun explicitly — and
that's fine. Chat is their primary surface.

**Week 2 — new extension.** The instructor pushes a domain extension
mid-semester (`pip install ne101-radprotect-lab`). On the student's
next `axi` invocation:

```bash
$ axi
  ↑ New extension installed: radprotect-lab (4 starter verbs)
  ↑ Try `axi help radprotect-lab` to see what's available.
```

Familiarity for `radprotect-lab` starts at `starter` even though the
student is comfortable with `chat` at `core` — the platform doesn't
flood them with the new lab's full surface.

**Month 2 — hand-off.** The student is mid-prep for an exam, runs
out of time, drops back into chat:

```bash
> What was I in the middle of?

  In the last 6 hours you ran:
    • axi classroom take --quiz=ch5-warmup (28 min ago, completed)
    • axi memory show "@laptop:student" (5 min ago)
    • You were skimming a worked example in unit 5.

  Want me to keep walking through unit 5, or summarize where you
  left off so you can resume tomorrow?
```

**Year 1 — power use** for a Student is rarely typing CLI verbs at
all. They live in chat. The `axi` surface is invisible to them by
design; the orchestrator and CHALKE do the work the verbs would.

### Journey: Instructor

> An NE 101 instructor preparing the same Prague course.

**Day 1 first task** is concrete — preparing course materials:

```bash
$ axi classroom prep --course=ne101 --source=./syllabus.md
  ✓ ingested 47 source documents
  ✓ generated 12 unit drafts
  ✓ created 6 quiz banks (drafts; review needed)
  → Review with: axi classroom proposals list
```

The instructor reviews proposals (one of the `core`-tier verbs from
day one — instructors enter at `core`, not `starter`, because the
PRD's persona table marks them at "Intermediate" baseline).

**Week 1 — discovery + graduation.** The instructor runs `axi
classroom serve`, monitors student questions via `axi memory show`,
and uses `axi pub overview` to track which materials are published.
After breadth + count thresholds clear, `advanced` verbs surface:
`axi classroom proposals push` (Canvas write-back), `axi nodes status`
(to confirm cohort federation health).

**Week 2 — new extension.** Adopting the `radprotect-lab` extension
the instructor authored *for* their students. Hot-load lets them
test the rollout from their own session before announcing to the
cohort.

**Month 2 — hand-off.** The instructor catches up Monday morning by
asking chat:

```bash
> What did I push to the cohort over the weekend?

  Friday 5pm:  axi classroom proposals push (3 quizzes, unit 5 draft)
  Sat 11am:    axi pub publish (worked-example handout v2)
  Sun:         no activity from this account.
  Note: 14 student turns are awaiting your review (axi classroom me --review).
```

**Year 1 — power use.** The instructor moves between `axi` (heavy
authoring), chat (live monitoring + hand-off), and the cross-harness
shims (Claude Code for course-prep editing). Per-extension
familiarity has graduated `classroom` to `advanced`; `federation` is
still `core` because they only touch it for cohort sync.

### Journey: Extension Author / Builder

> A developer building a new domain extension for their lab.

**Day 1 first task**: scaffolding.

```bash
$ axi ext init lab-spectrometry --tier=starter
  ✓ scaffolded src/lab_spectrometry/ per AEOS spec
  ✓ axiom-extension.toml at builtin tier
  → Next: axi ext lint
```

**Week 1 — discovery + graduation.** Author rapidly cycles through
`axi ext init`, `axi ext lint`, `axi review` (REV-U on local diff),
`axi commands generate` (cross-harness shim refresh after each verb
addition). Their `commands` extension familiarity advances first;
then `review`; then `ext`.

**Week 2 — new extension.** Author installs a peer's `lab-imaging`
extension to test interop. Even at `advanced` for their own
extension, `lab-imaging` enters at `starter` — they need to *learn*
the peer's verbs before the platform reveals the deep surface.

**Month 2 — hand-off.** Author hands review of a long-running build
to chat:

```bash
> Summarize what's left on lab-spectrometry before I tag v0.2.

  Last 24h:
    • axi ext lint            (3 manifest warnings, all in calibration/)
    • axi review --base main  (REV-U flagged 2 minor + 1 doc nit)
    • axi release list watched      (CI green; 1 PR awaiting your approve)
  Suggested next steps:
    1. Address the 2 minor REV-U findings in calibration/scan.py
    2. Approve PR #47 (the doc nit fix); RIVET will tag from there.
```

**Year 1 — power use.** Author operates at `advanced` for `ext`,
`review`, `commands`, `rivet`, and `tidy`; their muscle memory chains
verbs without thinking; the cross-harness shims are how their
collaborators (who don't know the verb tree yet) interact with the
extension.

### Sketches: other personas

**Researcher / Investigator.** Day 1 first verb is `axi research init
--project=…`, then `axi rag` to point at the project corpus. CURIO
takes over much of the literature-synthesis loop; the human's CLI
surface stays tight (`research`, `rag`, `memory`, `pub`, occasional
`federation` for cross-lab pulls). Power use looks like running
multi-day curation loops with `axi research chain` while CURIO
proposes distillations the human approves.

**Domain Operator / Practitioner.** Journey is shaped by the
*installed domain extension*, not by Axiom directly. A plant
control-room operator types `axi log query` and `axi log entry
create` daily; a factory-floor lead types `axi <factory-ext> shift
handoff` and `axi pub publish`; a field-deployment operator works
disconnected and reconciles on `axi federation sync`; a lab proctor
types `axi classroom serve` and `axi classroom proposals approve`.
The platform's job is to make the domain verbs feel native —
incremental revelation keeps the operator's attention on the domain,
not on Axiom.

**Federation Steward.** Enters at `core` or `advanced` from day one
(per the persona-table baseline) because their job *is* the platform.
Day-one verbs include `axi nodes add`, `axi federation join`, `axi
directive list`, `axi security status`. Power use is largely about
WARDEN delegation: setting trust-graph policies, reviewing peer
attestations, and rotating signing material on cadence. They live
where the platform meets the rest of the world.

---

## The Agent Team

Axiom agents follow an ALL-CAPS-HYPHEN naming convention — short, two-syllable, mechanical-feeling identifiers chosen to be quick to type and easy to address (`@name`). Each agent owns one job in the platform's REPL.

| Agent | CLI | Role | REPL Role |
|-------|-----|------|-----------|
| **AXI** | `axi chat` | Orchestrator — routes commands, delegates to other agents, maintains context | **Loop** |
| **SCAN** | `axi signal` | Event Evaluator — signal detection and intelligence extraction | **Read** |
| **CURIO** | `axi research` | Research agent — autonomous RAG optimization and knowledge synthesis | **Eval** |
| **TIDY** | `axi tidy` | Micro-Obliterator — resource stewardship and system hygiene | Infrastructure |
| **PRESS** | `axi pub` | Print agent — document lifecycle, .md → polished .docx → publish. Also owns content gating (formerly Mirror) | **Print** |
| **TRIAGE** | `axi doctor` | Defib — diagnostics, health, configuration audit. Also owns security checks (formerly SECUR-T) | Health |
| **RIVET** | `axi release` | Release agent — build, tag, ship | Ship |
| ~~Mirror~~ | ~~`axi mirror`~~ | Retired — agent gone, content gate absorbed by PRESS | — |
| ~~SECUR-T~~ | — | Retired — security ownership transferred to TRIAGE | — |
| ~~Neut~~ | — | Retired — was a domain-distribution rename of AXI; multiplied names without solving identity confusion. AXI is the orchestrator everywhere. A distribution remains a *distribution* (`pip install <distribution>`), not a renamed agent. |

Agents follow a **REPL model** (Read-Eval-Print-Loop): SCAN reads signals in, CURIO evaluates and synthesizes knowledge, PRESS prints publishable output, and AXI is the interactive loop that orchestrates the cycle.

### Tool, agent, distribution — three layers, one rule per layer

The CLI surface layers three identities. Conflating them creates the
confusion this PRD's identity-sharpening Phase 1 work resolves:

| Layer | What it is | Identity | Where the user encounters it |
|---|---|---|---|
| **Tool** | The CLI executable / entry point | `axi` (long form `axiom`) | Every shell invocation |
| **Orchestrator agent** | The conversational agent at `axi chat` | **AXI** (always; no per-distribution rename) | The chat session itself: welcome banner, speaker prefix, addressable as `@axi` |
| **Distribution** | A bundled product that ships `axi` plus a curated set of extensions | a domain consumer, Keplo, etc. | `pip install <name>`; the brand on marketing pages |

`axi` is the switchboard — typing it means "talk to the mothership." AXI is who picks up when the user opens a session. The distribution is the package they installed to get both. Each name points at exactly one thing. See [§Agent Addressing](#agent-addressing) for the syntax users employ when they want to address a specific agent rather than route through AXI.

---

## Shipped Commands

### Core Platform

| Command | Extension | Kind | Description |
|---------|-----------|------|-------------|
| `axiom config` | core | utility | Interactive onboarding wizard |
| `axiom status` | status | utility | System health dashboard |
| `axiom doctor` | triage (TRIAGE) | agent | Diagnose environment issues |
| `axiom ext` | core | utility | Manage extensions (builtin + user) |
| `axiom update` | update | utility | Dependency and migration updates |
| `axiom settings` | settings | utility | View and edit axiom settings |
| `axiom test` | test | utility | Test orchestration |

### Agents

| Command | Extension | Description |
|---------|-----------|-------------|
| `axiom chat` | axi_agent (Axi) | Interactive agent with tool calling. Alias: `axiom code` |
| `axiom signal` | signals (SCAN) | Agentic signal ingestion pipeline |
| `axiom pub` | publishing (PRESS) | Document publishing lifecycle. Alias: `axiom doc` |
| `axiom tidy` | hygiene (TIDY) | Resource steward — scratch, vitals, cleanup |
| ~~`axiom mirror`~~ | ~~mirror_agent~~ | Deprecated — content gate absorbed by PRESS |

### Tools & Services

| Command | Extension | Description |
|---------|-----------|-------------|
| `axiom db` | db | PostgreSQL + pgvector infrastructure |
| `axiom rag` | rag | RAG index management — index, search, sync the three-tier corpus |
| `axiom demo` | demo | Guided demonstrations and walkthroughs |
| `axiom note` | note | Quick personal notes — captured to RAG-indexed daily log |
| `axiom serve` | http | Start the axiom HTTP API server |

---

## AXI: The Orchestrator (`axi chat`)

AXI is the interactive agent — think Claude Code, but federation-
aware and built on top of the rest of the agent team (SCAN, TIDY, PRESS,
TRIAGE, RIVET, CURIO, …). AXI orchestrates the team and the
installed extensions on the user's behalf, and is addressable by name
when the user wants to bypass the orchestrator's default routing
(see [§Agent Addressing](#agent-addressing)).

### Interaction Modes

AXI operates in three modes that represent **levels of autonomy**, not different personalities. The default is Ask — the safest mode — and AXI escalates only with the user's consent.

| Mode | Autonomy | What AXI Does | Side Effects |
|------|----------|------------------|-------------|
| **Ask** (default) | None | Answers questions, explains, searches. Read-only. | Zero |
| **Plan** | Propose | Explores the problem, designs an approach, presents structured options. | Zero until approved |
| **Agent** | Execute | Runs multi-step tasks autonomously within scoped permissions. | Bounded writes |

### Escalation Model

AXI always starts in **Ask** mode. When a prompt implies action, AXI proposes escalation rather than assuming permission:

```
User: "Set up the next batch of work using the standard config"

AXI (Ask): I can help set that up. This will create a new record,
              reserve the requested resource(s), and generate the
              corresponding authorization or approval request.

              → Switch to Plan mode to walk through the steps? [y/N]
```

The user's actual prompt and the verbs AXI would invoke are shaped by
whichever domain extensions are installed: a plant-operations extension
might surface "shift / lockout / batch / authorization"; a classroom
extension might surface "course / cohort / module"; a field-deployment
extension might surface "mission / unit / supply-line / sync"; a devops
extension might surface "deploy / environment / runbook". The escalation
pattern is domain-agnostic.

Power users can skip escalation with `axiom chat --mode plan` or mid-conversation with `/plan`, `/ask`, `/agent`.

### Session & UX Flags

| Flag | Description |
|------|-------------|
| `--resume` | Continue the most recent session |
| `--session <id>` | Continue a specific session by ID |
| `--list` | List recent sessions (ID, title, messages, cost, last active) |
| `--sync` | Sync local JSON sessions to PostgreSQL |
| `--simple` | Read-only tools only (no writes) |
| `--tools <list>` | Restrict to named tools (comma-separated) |
| `--budget <tokens>` | Set per-session token budget |
| `--json` | Structured JSON output (no streaming, no rich rendering) |
| `--mode <ask\|plan\|agent>` | Start in a specific interaction mode |

Sessions are stored in PostgreSQL when available, with automatic fallback to local JSON files. See [spec-session-store.md](../specs/spec-session-store.md) for the backend and [spec-axi-cli.md §Chat Mode](../specs/spec-axi-cli.md) for detailed UX.

### Slash Commands

Slash commands are the composable action layer inside `axiom chat`. Each command is registered by an extension, follows a standard four-step flow (collect context → present choices → confirm intent → dispatch + report), and always shows the underlying `axi` CLI invocation it dispatched.

This design teaches users the machine API through the conversational interface — and is the concrete implementation of the broader **CLI ↔ Chat gravity** documented in [§CLI ↔ Chat Parity](#cli--chat-parity) below: when a CLI verb covers the user's intent, chat resolves to it through the same execution path the terminal uses (with every step shown before dispatch). Chat retains its full agent surface — MCP tool calls, multi-step composition — but biases hard toward CLI verbs whenever they apply, because that's what unlocks determinism, auditability, performance, and tier-respect.

Full slash command implementation list in [CLI Specification §Slash Commands](../specs/spec-axi-cli.md); the resolution algorithm in [CLI Specification §Chat ↔ CLI Resolution](../specs/spec-axi-cli.md).

### Mode Guardrails

| Guardrail | Behavior |
|-----------|----------|
| **Writes always confirm** | Even in Agent mode, AXI pauses before creating records, publishing, or sending notifications |
| **Agent scope is per-session** | Agent permissions don't persist across sessions |
| **Escalation is reversible** | `/ask` returns to read-only at any time |
| **Structured decisions** | Bounded option spaces use interactive forms, not freetext |
| **Audit trail** | All mode transitions and approved actions are logged |

---

## Planned Commands

These nouns are designed but not yet implemented. Each ships as an
extension and is therefore shaped by its domain — the verbs and flag
vocabulary below are illustrative, drawn from a mix of consumer domains.
One worked example uses a scientific-workload consumer (e.g. a nuclear-engineering consumer) where
a concrete system/code pair makes the shape clearest; parallel examples
from other domains appear alongside it. Other consumer domains shipped as
analogous extensions with their own vocabulary include classrooms,
research labs, factories and utility plants, field/military deployments,
company orgs, devops teams, and content pipelines (full list in
[§Problem Statement → Where Axiom is uniquely positioned](#where-axiom-is-uniquely-positioned)).

### Model Registry (`axiom model`)

Versioned model registry with validation, lineage, and audit. Domains
adapt the noun to whatever "model" means for them — physics simulators,
ML weights, design specs, course rubrics.

```bash
# Generic shape
axiom model search "<keywords>"
axiom model list --system=<system> --tag=<tag>
axiom model init ./my-model --kind=<kind>
axiom model validate ./my-model
axiom model add ./my-model --message="<change-summary>"
axiom model diff <id-a> <id-b>
axiom model lineage <id>
axiom model audit --since=2026-01-01
```

**Examples by domain:**

| Domain | Example invocation |
|---|---|
| Scientific workload (e.g. nuclear engineering) | `axiom model init ./my-model --system=<system> --code=<code>` |
| Research lab | `axiom model init ./my-classifier --kind=ml --framework=pytorch` |
| Classroom (Keplo) | `axiom model init ./rubric --kind=assessment --course=ne101` |

See [Model Corral PRD](prd-model-corral.md) for the platform-level
contract; consumer-domain repos for full command sets.

### Digital Twin Hosting (`axiom twin`)

Reduced-order-model execution, shadow runs, drift detection, and
prediction validation against measured ground truth.

```bash
# Generic shape
axiom twin run --model=<id> --type=<run-kind>
axiom twin shadow --target=<system> --date=<date>
axiom twin infer --model=<id> --input=<state>
axiom twin compare --run=<run-id> --against=measured
axiom twin drift --model=<id> --since=<date>
axiom twin report --target=<system> --period=<period>
```

The shape is domain-agnostic — twins are useful anywhere a measured
system can be modeled and the model's predictions can be checked
against reality. See consumer-domain repos for vocabulary.

### System Operations Log (`axiom log`)

Time-series of operational events with structured metadata, search,
export, and shift-handoff support.

```bash
# Generic shape
axiom log query --last 1h
axiom log query --type=<entry-type> --since=<date>
axiom log entry create --type=<entry-type> --content="<note>"
axiom log export --format=<format> --range=<from>:<to> -o report.pdf
```

Useful anywhere with operational regularity — plant control rooms,
factory shift handoffs, SRE on-call rotations, field-deployment
status updates, classroom session cycles, lab maintenance windows.
See consumer-domain repos for the entry-type vocabulary they expose.

### Data Platform (`axiom data`)

Lakehouse queries, pipeline orchestration, backfills.

```bash
axiom data query "SELECT * FROM <table> LIMIT 10"
axiom data pipeline status
axiom data backfill <table> --start <date>
```

Domain-agnostic.

### Connections (`axiom connect`)

Unified credential and endpoint management for external integrations.

```bash
axiom connect teams --method browser
axiom connect --check
axiom connect --json
```

See [Connections PRD](prd-connections.md).

### Agent Lifecycle (`axiom agents`)

Register, start, stop, and monitor background agent processes.

```bash
axiom agents register-launchd          # macOS
axiom agents start scan
axiom agents stop tidy
axiom agents status
axiom agents logs scan --since 1h
```

See [Agents PRD](prd-agents.md).

### State Management (`axiom state`)

Inventory, backup, restore, and retention across all state locations.

```bash
axiom state inventory --verbose
axiom state backup --encrypt --output backup.tar.gz
axiom state restore backup.tar.gz
axiom state retention --status
axiom state cleanup --dry-run
```

See [Agent State Management PRD](prd-agent-state-management.md).

### Media Library (`axiom media`)

Cross-cutting media management — recordings, photos, documents — with
tagging and linking to other Axiom entities (entries, experiments,
courses, deployments, whatever the installed extensions expose).

```bash
# Generic shape
axiom media ingest <path> --tag=<tag> --link=<entity:id>
axiom media search "<query>" --type=<audio|image|video|doc>
axiom media tag <id> <tag>...
axiom media link <id> <entity:id>
```

Examples by domain include attaching a control-room photo to an ops
log entry, attaching a lecture recording to a class session, attaching
a build-artifact screenshot to a release tag, etc.

See [Media Library PRD](prd-media-library.md).

### Infrastructure (`axiom infra`)

Service health, logs, deployment orchestration.

```bash
axiom infra health
axiom infra logs axiom-gateway --since 1h
axiom infra deploy --env staging
```

---

## Non-Functional Requirements

| Requirement | Target |
|-------------|--------|
| **Startup time** | <100ms (no network) |
| **Authentication** | OAuth2/OIDC, API keys, certificate |
| **Output formats** | Table, JSON, YAML, CSV |
| **Shell completion** | bash, zsh, fish, PowerShell |
| **Offline mode** | Graceful degradation for local operations |
| **Cross-platform** | macOS, Linux, Windows |

---

## User Experience Principles

### Progressive Disclosure

> **Status:** AEOS schema fields ✅ shipped; consumers (smart help,
> familiarity tracking, generator filtering) 🟦 designed → Phase 1.
> See [§Status & Phasing](#status--phasing).

Axiom rolls up dozens of extensions, each with multiple verbs, into a single
`axi <noun> <verb>` namespace. A novice user shown 38 nouns and 200+ verbs at
once will bounce. A senior user shown only the starter subset will feel
infantilized. The CLI reconciles this with three layered mechanisms
(capability tiers + per-extension familiarity + intent groups), backed by
adaptive smart help.

#### Capability tiers (per verb, manifest-declared)

Every verb declares one of four tiers in its extension manifest:

| Tier | Audience | Examples |
|---|---|---|
| `starter` | First-run novice | `axi chat`, `axi doctor`, `axi update`, `axi hygiene status` |
| `core` | Day-to-day operator | `axi classroom prep`, `axi hygiene list worktrees`, `axi connect`, `axi memory show` |
| `advanced` | Power user, admin | `axi nodes …`, `axi federation invite`, `axi hygiene purge`, `axi model audit` |
| `internal` | Debug, dev, dangerous | `axi hygiene diagnose`, `axi rag rebuild --force`, `axi state restore` |

Tiers cumulate: `starter ⊂ core ⊂ advanced ⊂ internal`. `axi help` defaults
to surfacing only the tier the user has reached. `axi help --all` shows
everything when explicitly requested.

#### Per-extension familiarity (handles foreign surfaces)

A user fluent in the rest of Axiom is still a *novice* when a new extension
is installed — they've never seen its verbs. The system tracks familiarity
**per extension**, not just globally:

- A new extension always starts at `starter` for that user, regardless of
  global tier.
- Familiarity grows as the user invokes verbs from that extension
  successfully and explores its breadth.
- Effective surfacing for any verb = `min(global_tier, per_extension_familiarity)`.

This means installing a third-party `<foo>` extension surfaces a small,
discoverable subset (its `starter` verbs) even for an expert user, with
graduated revelation as they use it.

#### Hands-free graduation

Tier elevation is **earned, not configured**. The system observes:

- Successful invocation count per verb and per noun
- Variety of verbs used across the extension (breadth of coverage)
- Successful invocation of an `advanced` verb directly (with a
  confirm-and-document handoff)
- Time since first encounter

When competency thresholds clear, the next tier becomes visible silently —
new verbs appear in `axi help` without ceremony. The user can also pull
verbs forward explicitly with `axi help --tier core` or
`axi help --all`, but never has to.

The tier model also exposes the same signals to the **generator** that
emits cross-harness slash-command shims (Claude Code, Cursor, VS Code,
etc.) so those surfaces don't dump the entire Axiom command space into
each editor; see `prd-commands-generator.md`.

#### Reveal: when the user wants more than the system thinks they're ready for

The surfacing rule above gates *automatic presentation*, not
*invocation*. Typing the full `axi <noun> <verb>` always works,
regardless of tier — that's the safety floor that makes the rest of
the model honest. But a user who *knows* they want the wider surface
shouldn't have to wait for the graduation thresholds to clear. This
is what the reveal mechanism is for: invisible by default, accessible
through the three natural discovery vectors.

**The user asks** — explicit help flag, scoped to one extension:

```bash
$ axi help radprotect             # default: only starter verbs
$ axi help radprotect --all       # full surface for radprotect
$ axi help radprotect --tier core # widen to a specific tier
```

**The user is told** — chat surfaces the reveal hint at the moment of
need, when resolving an intent that maps to a hidden verb. The hint
appears inside the existing resolution-preview pattern; chat does not
refuse to invoke a hidden verb, it just notes the threshold and
suggests the wider view.

**The user stumbles across it** — three passive single-line surfaces:

- `axi help <ext>` footer: `… 14 more in advanced/internal · 'axi help radprotect --all' to reveal` (dim; only appears when the count materially exceeds what's shown).
- `axi ext install` post-install hint mentions the reveal flag once.
- Tab-twice widening: `axi radprotect <Tab><Tab>` widens that
  session's completion to all tiers for that extension (honors the
  zsh/fish "tab-twice = show more" convention; bash falls back to
  the explicit `--all` form).

**Persistence.** A reveal sticks for the session by default. The user
promotes it explicitly:

```bash
$ axi help radprotect --all --pin           # persist for this user
$ axi competency set radprotect advanced    # explicit familiarity claim
```

Either writes to `~/.axi/competency.json`; the graduation observer
respects pinned values and won't roll them back.

**`internal` tier requires one extra step.** `internal` verbs (debug,
dangerous, dev-only) are hidden behind `--all --internal` rather than
just `--all` — one extra deliberate keystroke before the riskiest
surface appears.

**Reveal as competency signal.** Every reveal is recorded to the CLI
history with `via=reveal`. The graduation observer counts user-driven
reveals followed by successful invocations as accelerated familiarity
signals — the system *learns* that this user is comfortable here, and
the reveal hints stop appearing once familiarity has caught up to
where the reveals showed they were already operating.

#### Intent groups (cross-extension rollups)

Capability tiers answer "is this verb appropriate for this user yet."
Intent groups answer "which verb does this user want right now." Verbs
declare which user-task they belong to (a verb may belong to several):

| Group | Composition example (across extensions) |
|---|---|
| `start` | `chat`, `doctor`, `update`, `status`, `install` |
| `teach` | `classroom prep`, `classroom serve`, `classroom canvas pull`, `classroom proposals …` |
| `maintain` | `tidy status`, `tidy clean`, `tidy worktrees`, `rivet watched`, `triage heartbeat` |
| `investigate` | `memory show`, `knowledge report`, `signal …`, `tidy diagnose` |
| `build` | `ext init`, `ext lint`, `ext install`, `commands generate` |
| `govern` | `nodes …`, `federation …`, `directive …`, `security …` |
| `research` | `research …`, `model corral`, `rag …` |

`axi help <group>` shows the rolled-up view across extensions for a
user-task — beats forcing the user to remember which extension owns what.

#### Smart, dynamic help

`axi help` is adaptive, not static. It draws on:

- The user's current capability tier and per-extension familiarity
- Recent invocation history (see *CLI ↔ Chat Parity*)
- Common chains observed in this user's usage (e.g. "after `classroom prep`
  you typically ran `classroom serve`")
- The current working directory (e.g. surfacing `tidy worktrees` when in a
  git repo with stale worktrees)

First-run users see the `start` group with two-line "what's next" hints.
Returning users see what they've used + the next-step verbs. Errors
include context-aware suggestions ("Did you mean `tidy worktrees`? You're in
`~/Projects/x` which is a git repo with 3 stale worktrees.").

#### Flag defaults complement command surfacing

Within an exposed verb, sensible defaults still apply:

```bash
# Simple (sensible defaults)
axi log query

# Detailed (when needed)
axi log query --type=<entry-type> --target=<system> --since=2026-01-20T08:00:00Z --format=json
```

Tiers + intent groups + smart help govern *which* verbs appear; flag
defaults govern *how much detail* is required once a verb is invoked.

### CLI ↔ Chat Parity

> **Status:** 🟡 Partial — chat REPL ships today with slash commands and
> tool calling; the resolution-path invariant + shared-history hand-off
> are 🟦 designed → Phase 2. See [§Status & Phasing](#status--phasing).

`axi chat` is a full LLM-mediated agent — it has access to its CLI
verb tree, its attached MCP tools, and the ability to compose
multi-step plans across both. What it is **not** is a parallel API
that diverges from the CLI: when a CLI verb covers the intent,
chat resolves to that verb through the same execution path the
terminal uses. The bias toward CLI is what we call **CLI gravity**,
and it's what unlocks determinism, auditability, performance, and
tier-respect for the work that flows through it.

The resolution algorithm with the full plan-shape grammar is
specified in [`spec-axi-cli.md §Chat ↔ CLI Resolution`](../specs/spec-axi-cli.md#chat--cli-resolution);
the high-leverage version is the [§Chat resolution path](#chat-resolution-path--cli-gravity-not-cli-exclusivity)
below.

#### Invariants

- **Behind-the-scenes state is shared.** Chat does not have privileged
  data, separate write paths, or a parallel implementation of any
  capability that already has a CLI verb. A chat-issued
  `axi hygiene list worktrees --prune` and a terminal-issued one are
  indistinguishable to every other component.
- **Chat shows every step of its plan** before any of it dispatches —
  CLI invocations and tool calls alike. The user sees the literal
  CLI command (or the literal tool call with its args) before
  approving (consistent with the slash-command four-step flow:
  collect → present → confirm → dispatch + report).
- **CLI gravity** — when a CLI verb covers the intent, chat resolves
  to it; chat does not synthesize an equivalent sequence of tool
  calls when the verb already exists.
- **Provenance distinguishes the dispatch path**, not the intent.
  Each step in chat's plan records its own provenance:
  `via=chat` for chat-resolved CLI invocations, `via=chat:tool` for
  MCP tool calls, `via=chat:plan(N/M)` for steps in a sequence.
  This makes "what did chat synthesize that wasn't a CLI
  invocation?" a useful audit query — both for finding gaps where
  a new CLI verb would help, and for investigating drift.

#### Shared history (the "hand-off" property)

Every `axi` invocation — from terminal or chat — appends to a unified
history stream. When a user runs `axi chat`, the chat session ingests
recent history so the user can hand work in mid-stream without
re-explaining context:

> User: "What was I in the middle of?"
> Chat: "You ran `axi classroom prep` 12 minutes ago, then started
> `axi classroom serve` but ctrl-C'd before it finished initializing.
> Want me to pick up where you left off?"

#### Memory-backed history with decay

The history stream lives behind a memory-provider abstraction:

- **Default**: Axiom Memory's MIRIX `episodic` tier provides durable,
  cross-session storage. Axiom Memory naturally supports decay through
  tier transitions (episodic → semantic → archive), so older invocations
  don't accumulate unboundedly — they consolidate into patterns
  (procedural memory: "user typically runs A then B").
- **Fallback**: a slim SQLite history table for installs that don't yet
  have the full memory layer wired up. SQLite is universally present per
  the install model, so this fallback is always available.
- **Configurable retention** with reasonable defaults (last 30 days at
  full fidelity; older entries decay into aggregated patterns). The
  decay curve is intended to become adaptive over time as the system
  learns which historical detail proves most valuable for context
  reconstruction.

#### Cross-harness session mirroring

> **Status:** 🔵 Strategic — `axi chat` ↔ canonical transcript store
> at Phase 2; cross-harness adapters (Claude Code, Cursor, Codex,
> OpenCode) at Phase 3. Full design in `spec-cross-harness-mirroring.md`
> (TBD post-Prague).

The CLI ↔ Chat parity model so far covers `axi <noun> <verb>` (terminal)
and `axi chat` (REPL) sharing one history stream. But Axiom users live
in many harnesses — Claude Code, Cursor, Codex, OpenCode, and others
the cross-harness shim generator (`prd-commands-generator.md`) already
serves at the command-invocation layer. The conversation surface should
extend the same way: a user moving between `axi chat` and Claude Code
shouldn't have to re-explain what they were just doing.

The canonical answer is **opt-in session mirroring** through a shared
transcript schema, written behind the same memory-provider abstraction
as the CLI history.

##### What gets recorded

Per turn:
- Speaker + role (`user` / `agent`, with agent identity — AXI,
  Claude, Cursor, etc.)
- Content (user input, agent response, tool invocations, tool
  results)
- Provenance (`harness=`, `model=`, `session_id=`, `cwd=`, `repo=`,
  `via=`)
- Cost + usage (tokens, model tier, duration)
- Privacy class (default normal; user can mark a turn `private` or
  `redacted`)

Same secret-pattern denylist as the CLI history (sk- / Bearer /
org- / KEY= patterns).

##### Where it lives

Behind the same memory-provider abstraction the CLI history already
uses (per [§CLI History and Decay](../specs/spec-axi-cli.md#cli-history-and-decay)):
**Axiom Memory MIRIX `episodic` tier** by default; SQLite fallback
for slim installs; federated memory for cohort-shared transcripts
under joint provenance (ADR-027).

The transcript shape is canonical; per-harness adapters write into
it.

##### How each harness opts in (the two directions are asymmetric)

Mirroring runs in both directions, but the mechanics differ. Axiom
controls its own surface; for external harnesses Axiom can only
**publish** (and trust the harness to use what's published). So the
inbound and outbound paths each get their own opt-in mechanism.

**Inbound — External harness → Axiom Memory**

| Harness | Mechanism |
|---|---|
| `axi chat` | Native write; on by default; `--no-mirror` per session, `/private` slash command per turn |
| Claude Code | When the Axiom MCP server (`axi mcp serve`) is registered, CC publishes session events through `chat.transcript.publish` |
| Cursor / Codex / OpenCode | Same MCP-based publish |
| Pure terminal `axi <verb>` | Already covered by CLI history |

**Outbound — Axiom Memory → External harness**

The Axiom MCP server exposes the read side of the same channel; each
external harness then surfaces it according to its own conventions.
Axiom can't reach into Claude Code or Cursor to *force* surfacing —
it can only make the data and the prompts available, and lean on
the per-harness shim files (already emitted by `axi commands
generate`) to make activation idiomatic.

| Harness | Mechanism (read side) |
|---|---|
| `axi chat` | Native ingest of recent transcripts at session start (per [§Shared history](#cli--chat-parity)) |
| Claude Code | Two surfaces: (a) MCP tool `chat.transcript.recent` callable by the LLM on demand; (b) the generated slash command `.claude/commands/axi/chat/resume.md` — `/axi chat resume` — pulls recent Axiom session and asks the user "continue this here?" |
| Cursor | Tools-menu entry from the same MCP server (`chat.transcript.recent`); the flat shim `.cursor/commands/axi-chat-resume.md` provides the explicit invocation |
| Codex / OpenCode | MCP-only — the tool surfaces contextually; user invokes via natural language ("pull my recent axi session") |
| Vim / Neovim | The generated `:Axi chat resume` user command issues the same MCP call and prints into a scratch buffer for the user to paste / reference |

The asymmetry is real: in the inbound direction Axiom *receives* and
can enforce volume controls + compaction (above); in the outbound
direction Axiom *publishes* and has to trust the receiving harness
not to pollute its own context window. The provenance discipline
below is what keeps that trust enforceable: every turn carried
across the bridge cites its source, in either direction, so the
receiving harness — and the human — can always see what was
borrowed.

##### How cohesion is maintained when the user switches harnesses

Two flavors of handoff:

- **Checkpoint-on-pause** (default, low cost): user finishes a work
  block in one harness; that harness publishes a transcript summary
  + key decisions; the other harness ingests it on next session
  start, surfaces it as recoverable context (*"I see you were just
  working in Claude Code on X. Continue here, or start fresh?"*),
  and the user accepts or declines.
- **Real-time stream** (opt-in, higher cost): turn-by-turn fanout
  as each turn lands in any participating harness; useful for
  mid-stream handoff rather than mid-pause. Carried by the same
  A2A primitives that carry agent traffic.

##### Provenance discipline

No harness ever claims to be the other. AXI surfacing borrowed
context cites the source ("Per your Claude Code session 12 minutes
ago…") rather than presenting it as its own thinking. Claude Code
surfacing Axiom transcript context cites Axiom. The `via=` field on
each turn is load-bearing for this discipline; UI affordances on
both sides of the bridge use it to render attribution clearly.

##### Federation composition

When P2P chat ships (per [§Looking ahead](#looking-ahead-peer-to-peer-chat-roadmap-intent)),
the same mirroring substrate carries cross-node session sharing — a
user invites `@alice:ut-austin` into their session, Alice's preferred
harness sees the joint transcript with everyone's provenance
preserved. Mirroring is the substrate; P2P chat is one of its
consumers.

##### Volume management — the silent-sink risk

Mirroring implies a continuous **silent sink** of conversation
content into Axiom memory. At even modest scale (one user, two
harnesses, daily use) this accumulates fast. Without explicit
volume controls and compaction the sink degrades into a liability:
storage grows unboundedly, embedding and indexing costs grow with
it, retrieval quality drops as signal-to-noise erodes, and chat's
hand-off ingest itself starts surfacing low-value context.

This PRD commits to four volume-management controls **before**
cross-harness mirroring ships beyond Phase 2 native `axi chat`:

1. **Per-harness rate ceiling.** Each adapter declares a maximum
   turns/hour; exceeding the ceiling triggers summary-only mode for
   that harness rather than full-transcript writes.
2. **Per-user quota with eviction.** A bounded total turn count
   across all sources before old entries roll into the next memory
   tier (episodic → semantic → archive). Default: 30-day window at
   full fidelity, configurable.
3. **Auto-summarization at write time, not just read time.** When
   a turn is sunk into Axiom Memory, an inline summarizer also
   writes a 1-line digest tagged for procedural memory; the
   verbose original decays per the standard schedule, the digest
   persists.
4. **Visibility into what's been sunk.** `axi hygiene stat mem` (a new
   invocation of the canonical TIDY `stat` verb) surfaces sink rates
   per harness, cumulative storage by source, and the ability to
   purge a session, a date range, or a harness wholesale. The
   `axi hygiene stat <resource>` shape is the canonical TIDY pattern for
   resource observability — it reads as "stats about `<resource>`",
   composes for multi-resource queries (`axi hygiene stat mem disk net`),
   and never collides with verbs that read or write the underlying
   resource (e.g. `axi memory show` reads memory contents;
   `axi hygiene stat mem` reports on memory *volume and behavior*).

##### Compaction — explicit, not aspirational

Compaction across the memory tiers is a known doc gap — the
existing [§CLI History and Decay](../specs/spec-axi-cli.md#cli-history-and-decay)
section names a 30/90-day decay schedule and "adaptive curve" as
intent, but doesn't yet specify *who triggers compaction*, *when*,
*with what guarantees*, or *how the user observes and overrides it*.
That gap blocks responsible mirroring. This PRD elevates compaction
to a Phase 2 deliverable (was implicitly Phase 3) and queues a
dedicated **`spec-memory-compaction.md`** to cover:

- Compaction triggers (scheduled, threshold-based, event-driven)
- Tier-transition policy (episodic → semantic → archive)
- Per-tier retention guarantees + user-overridable knobs
- Failure modes (what if the summarizer is unavailable, what if the
  store is full, what if a compacted entry is later subpoenaed)
- Observability (`axi hygiene stat mem --compaction`, audit trail of
  what compacted to what when)
- Adaptive learning of the decay curve (how the system observes
  which historical detail proves most useful for context
  reconstruction and tunes from there)

The dependency direction is firm: **mirroring beyond Phase 2 native
`axi chat` is gated on the compaction spec landing first**.

##### Out of scope for this PRD (queued for the dedicated specs)

- Wire format of the canonical transcript schema
  → `spec-cross-harness-mirroring.md`
- Conflict resolution when the user makes parallel changes in two
  harnesses simultaneously
  → `spec-cross-harness-mirroring.md`
- Default policy: opt-in vs opt-out per harness
  → `spec-cross-harness-mirroring.md`
- Granularity tuning: full-transcript vs summary-only vs key-decisions-only
  → `spec-cross-harness-mirroring.md`
- Latency target for real-time fanout
  → `spec-cross-harness-mirroring.md`
- Cross-org redaction policy (when a transcript crosses cohorts via
  federation)
  → `spec-cross-harness-mirroring.md`
- Compaction architecture (triggers, tier transitions, retention
  guarantees, observability, adaptive learning)
  → `spec-memory-compaction.md` (Phase 2, gates Phase 3 mirroring)

What this PRD commits to today: the **abstraction** (canonical
transcript schema behind the memory-provider interface), the
**provenance discipline** (every turn cites its source), and the
**volume-management posture** (silent sinks always have explicit
ceilings, summarization, and a hygiene surface) won't change between
phases — implementation lands incrementally on this foundation.

#### Chat resolution path — CLI gravity, not CLI exclusivity

Chat is not restricted to invoking only verbs that have a CLI
equivalent. It is an LLM-mediated agent with access to its full tool
surface: Axiom's CLI verbs, attached MCP tools (Linear, Calendar,
custom domain MCPs, …), and the ability to compose multi-step
sequences across all of them. What this section specifies is the
**bias** chat applies when a CLI command does cover the user's
intent.

**The gravity rule.** When a user's intent maps to an existing
`axi <noun> <verb>` invocation, chat strongly prefers resolving to
that CLI command rather than synthesizing an equivalent sequence of
tool calls. The bias is intentional and earns its weight from four
properties:

- **Determinism** — CLI verbs have known semantics, fixed argument
  shape, reproducible behavior. Ad-hoc tool sequences don't.
- **Compliance + auditability** — CLI invocations land in the unified
  history with `via=chat`, the verb's own logs, and the platform's
  RACI ledger. Ad-hoc tool calls are harder to attribute.
- **Performance** — CLI verbs already wrap caching, batching,
  federation routing optimizations the LLM would otherwise
  re-discover at runtime.
- **Tier respect** — CLI verbs carry the surfacing tier the user has
  earned; ad-hoc tool calls bypass that signal.

**The chat session's job for any user input:**

1. **Parse intent** — what does the user want to do?
2. **Resolve to a plan**, which is one of:
   - A single `axi <noun> <verb>` invocation (the high-confidence
     CLI-gravity case)
   - A multi-step CLI sequence (chat is the planner; each step is a
     first-class CLI invocation)
   - A mixed plan that combines CLI invocations with MCP tool calls
     (e.g. read via `axi release list watched`, write via Linear's
     `create_issue` tool)
   - A pure tool plan when no CLI surface covers the intent (e.g.
     drafting a Slack message, querying Linear)
3. **Display the resolved plan** — every step shown literally
   (the CLI invocation as written, the tool call with its args).
4. **Confirm** per RACI rules and the user's current consent posture.
5. **Dispatch** — CLI steps go through the shared `axi.run(...)`
   path; tool steps go through their native handlers.
6. **Report** with the same formatter as the CLI for CLI steps;
   tool-native rendering for tool steps. Each step records its own
   provenance.

**What chat does NOT do, even in mixed plans:**

- Fabricate a CLI invocation that doesn't exist (closest existing
  verb surfaces as a discoverability hint instead — never an
  invented one)
- Bypass the tier model by substituting a tool call for an
  `internal`-tier verb without going through the reveal flow
- Hide the plan (every CLI step + every tool call is in the
  preview before any of them dispatch)

**The drift-prevention invariant** (the property that keeps chat from
becoming a parallel API):

> Whenever a CLI verb covers the intent, chat resolves to it. When
> chat composes beyond the CLI surface, every step is shown to the
> user, every step's provenance is recorded, and every CLI step
> goes through the same dispatch path the terminal uses. The CLI
> remains the canonical surface; chat is the surface that *reaches*
> it the most flexibly.

For the formal resolution path with the full plan-shape grammar and
provenance tagging (`via=chat:tool`, `via=chat:plan(N/M)`, etc.) see
[`spec-axi-cli.md §Chat ↔ CLI Resolution`](../specs/spec-axi-cli.md#chat--cli-resolution).

### Agent Addressing

> **Status:** 🟦 Designed → Phase 1 (implicit + explicit local) /
> Phase 2 (federated cross-node). See [§Status & Phasing](#status--phasing).

The CLI exposes one switchboard (`axi`) and a team of agents behind it.
Most of the time the user types `axi <noun> <verb>` and the platform
routes — they never need to know which agent backed the action. When
they *do* want to address a specific agent, the syntax is consistent
with the rest of the platform's principal-naming rule (`@<entity>:<server>`,
single `@`, Matrix-style — see `feedback_principal_naming` and
[`spec-axi-cli.md §Chat ↔ CLI Resolution`](../specs/spec-axi-cli.md#chat--cli-resolution)).

#### Three addressing modes

| Mode | Syntax | When to use |
|---|---|---|
| **Implicit** | `axi <noun> <verb>` (terminal) or natural language (chat) | The default. The platform routes to the agent that owns the verb / intent. |
| **Explicit (local)** | `@<agent>` prefix in chat, or `axi chat --to @<agent>` from terminal | The user wants a specific agent's voice without the orchestrator's routing. |
| **Federated (cross-node)** | `@<agent>:<server>` | The user wants an agent on a peer node — for cross-cohort consultation, federated chat, multi-node debug. |

#### Examples

```
# Implicit — most common
$ axi hygiene list worktrees                              # TIDY answers via the verb
> what's stale in this workspace?               # AXI routes to TIDY behind the scenes

# Explicit, local — bypass the orchestrator
> @tidy sweep this workspace and tell me what's stale
> @scan any signals on the federated leak topic this week?
$ axi chat --to @scan

# Federated — cross-node
$ axi chat --include @axi:bens,@tidy:ut-austin
> @axi:bens, can you look at our latest classroom prep run?
```

#### Why this composes

- **One addressing rule** across humans and agents: `@<entity>:<server>`.
  Ben on his laptop is `@ben:laptop`; AXI on Ben's federation node is
  `@axi:bens`. No special-case grammar for agents.
- **The tool name (`axi`) never appears inside an `@`-handle.** That's
  the structural reason a user can never confuse the switchboard with
  a participant: the switchboard is the door, the participants are
  whoever you meet on the other side.
- **Implicit-first** — 99% of users never type an `@`-handle. The
  syntax is there for the day they need it without inventing new
  surface.
- **The address grammar is i18n-clean; the rendering layer carries
  the possessive feel.** Human-friendly possessive forms ("Ben's
  AXI") don't round-trip through a single address grammar — English
  uses `-'s`, French uses `de`, Japanese uses `の`, Chinese uses `的`,
  German uses dative case. The canonical address stays Matrix-style
  (`@axi:bens`); the **rendering layer** translates that into the
  user's locale-appropriate possessive form for help text, error
  messages, chat speaker prefixes, and federation participant lists.
  A future i18n pass localizes the rendering without ever touching
  stored handles, log records, or A2A traffic.

#### Tab-completion across the address grammar

`@`-handles are only usable if they're discoverable. Tab-completion
on `@`-handles is required everywhere the user can type an address —
chat REPL, terminal `--to` flags, slash commands that take an address.
Completion is **federation-aware** by design, because the same syntax
addresses local entities and peer-node entities. Three completion
modes cascade as the user types:

| What user typed | Completion shows |
|---|---|
| `@<Tab>` | All addressable entities reachable from this node, ranked: recent / frequent / online first. Local agents (`@axi`, `@tidy`, …), local human principals, *and* federated peers (humans + agents on cohort peers) appear in one list. Online status surfaced (`✓` online · `·` offline · `⚠` unknown). |
| `@<entity><Tab>` | Server suffixes for that entity. E.g. `@axi<Tab>` → `@axi:laptop`, `@axi:bens`, `@axi:ut-austin` — each AXI instance reachable from this node. |
| `@:<server><Tab>` *or* `@:<Tab>` | Entities scoped to a known server. E.g. `@:bens<Tab>` → `@axi:bens`, `@tidy:bens`, `@ben:bens` — everyone on Ben's federation node. |

Completion data comes from the federation peer registry (the same
registry the cohort layer maintains). Queries are cached locally with
a TTL so tab-completion stays snappy even when peers are slow to
respond. The completion script respects the same RACI visibility
rules as the rest of the platform — a peer that hasn't authorized
you to see them doesn't appear in your completion list.

This completion surface is the **prerequisite** for the next thing
the addressing grammar consumes: peer-to-peer chat between humans
(see below).

#### Looking ahead: peer-to-peer chat (roadmap intent)

> **Status:** 🔵 Strategic → Phase 3. Placeholder only; full design
> in `spec-peer-to-peer-chat.md` (TBD).

The addressing surface specified above isn't only for routing to
agents — it is the **substrate for native peer-to-peer chat across
the federation**. Once the spec lands, the same `@<entity>:<server>`
grammar lets a user pull any combination of remote participants into
their current chat:

- **A remote human** — `/invite @alice:ut-austin`
- **A remote agent** — `/invite @axi:bens`
- **Both at once** — `/invite @alice:ut-austin,@axi:bens`

The result is a shared room where each participant operates with
their own provenance: Alice's turns are signed by `@alice:ut-austin`
and stored in her node's memory; her AXI's turns are signed by
`@axi:ut-austin`; Ben's turns and Ben's AXI's turns are signed
under his own node. The cohort registry mediates membership, A2A
carries the messages, the trust graph (ADR-028) governs who can be
invited by whom, and the joint-provenance memory model (ADR-027)
keeps each participant's contributions correctly attributed.

What this PRD commits to **today**, ahead of that spec:

1. The address grammar (`@<entity>:<server>`) won't change between
   "agent routing" use and "P2P chat" use. Muscle memory built now
   carries over.
2. The completion surface (federation-aware tab-completion above) is
   the *enabling* affordance for P2P chat — without it, inviting a
   remote participant requires remembering exact handles, which won't
   scale past a couple of peers.
3. The same identity, provenance, and trust primitives that route
   agent traffic federate human-to-human chat without a parallel
   stack — no new substrate to build, just a new surface to spec.

P2P chat itself — message storage policy across cohorts, room
lifecycle, RACI for who can DM whom, mute/block semantics, agent
participation rules, history-decay across multiple participants'
memory tiers, e2e signing — is **explicitly out of scope** for this
PRD. It is the next major design conversation after Prague, and it
gets its own document.

### Hot-load and hot-swap of extensions

> **Status:** 🟦 Designed → Phase 2 (Python loader); 🔵 Strategic → Phase 3
> (WASM-backed loader). See [§Status & Phasing](#status--phasing) and
> [`spec-extension-loading.md`](../specs/spec-extension-loading.md).

Installing a new extension should be felt **immediately** — not after a
CLI restart, not after a shell re-source. From the user's perspective:

```bash
$ axi ext install community-radprotect
  ✓ installed in 2.1s
  ✓ 6 new verbs available now: radprotect {scan, calibrate, history, …}
  ✓ shims refreshed for: claude, vscode
$ axi radprotect <Tab>
calibrate  history  scan  …
$ # no restart, no resourcing
```

The same applies to upgrades and removals — the running `axi` process,
any active `axi chat` session, and every long-running daemon (`rivet`,
`tidy`, `scan`, `triage`) recognize the change on its next iteration.

#### Requirements

- **Mid-session discovery.** Inside `axi chat`, an extension installed
  in another terminal becomes invocable on the next user turn — without
  ending the chat session.
- **Daemon hot-rebind.** Long-running agents pick up new hooks,
  subscribers, and capabilities on their next heartbeat or event-loop
  iteration.
- **Completion refresh.** Shell tab-completion reflects the new verbs
  on the next `<Tab>` (no `source ~/.bashrc` required).
- **Cross-harness shim refresh.** Previously-generated shims for
  Claude/Cursor/VS Code/Codex/etc. update automatically. This is the
  same `regenerate` hook described under *Reliable shell auto-complete*
  and `prd-commands-generator.md`.
- **Atomic install.** A failed install leaves no partial state.
  An interrupted install rolls back. Discovery never sees a half-loaded
  extension.
- **Drain-safe upgrade.** When an extension is upgraded mid-execution,
  in-flight invocations of the old version complete; new invocations
  pick up the new version. Daemons rebind on their next loop boundary,
  not in the middle of a critical section.

#### Implementation summary

- `axi ext install/upgrade/uninstall` are the **only** mutators of the
  installed-extension set. Each publishes a typed event
  (`ext.installed`, `ext.upgraded`, `ext.removed`) on the in-process
  event bus before exiting.
- A central **ExtensionRegistry** in the `axi` process tree subscribes
  to those events. On receipt it invalidates its cache, re-discovers,
  and republishes a `extensions.changed` event for downstream listeners
  (dispatcher, completion engine, chat session, daemons, generator).
- For long-running daemons in **separate processes**, the event bus's
  durable broker (or a dedicated state-watch file) propagates the event;
  daemons rebind on the next heartbeat tick.
- For **filesystem-side changes** that didn't go through `axi ext`
  (rare — manual `~/.axi/extensions/` edits), an opt-in filesystem
  watcher fires the same `extensions.changed` event so the system stays
  consistent.

The full protocol (event payloads, reload safety, drain semantics,
filesystem watcher, what *cannot* be hot-swapped, and the strategic
WASM-loader migration target) is specified in
[`spec-extension-loading.md`](../specs/spec-extension-loading.md).

#### Strategic direction: WASM-backed extension loader

The hot-load requirement above is the architecturally clean entry point
for migrating **the extension loader to a WASM Component Model runtime**.
Python's import system was not designed for safe reload — the host process
accumulates stale references, `importlib.reload` is partial, side-effects
of re-execution are unpredictable, and there is no per-extension isolation
boundary. A WASM loader sidesteps all of these:

- **True isolation per extension.** A WASM instance cannot reach into
  the host process or sibling extensions; capabilities are passed in
  explicitly via WIT-typed bindings.
- **Drain-safe upgrade is native.** Old and new instances of the same
  extension can coexist; in-flight invocations finish on the old
  instance while new invocations bind to the new — no reload tricks.
- **Polyglot extensions.** Third-party extensions can be authored in
  any language that compiles to a WASM Component (Rust, Go, C/C++,
  AssemblyScript, Python via py2wasm), expanding the contributor pool
  beyond Python developers.
- **Federation-friendly.** Verified WASM bytecode can travel between
  cohort nodes via the federation layer; an extension shipped from one
  node executes safely on a peer with only the capabilities its WIT
  declares. This composes cleanly with the trust graph (ADR-028) and
  signed-extension expectation in AEOS §6.
- **Capability-based security**, complementing RACI: WIT bindings make
  the host surface each extension can touch (memory writes, gateway
  calls, event bus, file I/O) explicit and reviewable.

**Phasing.** Hot-load v1 ships against the existing Python loader because
Prague (early June) requires a stable surface and the WASM migration is
foundation work that deserves its own ADR and a deliberate execution
window. WASM-backed loading is the **post-Prague evolution target**,
likely as a new manifest-declared `runtime = "wasm"` value alongside the
default `runtime = "python"`. Existing extensions stay Python; new ones
can opt in; performance-critical core paths can be ported one at a time.

[`spec-extension-loading.md §6`](../specs/spec-extension-loading.md)
sketches the WIT host surface, the per-extension manifest field
(`runtime = "wasm"`), the migration mechanics, and the open questions
(cold-start cost, WIT version pinning, signing pipeline). A dedicated
ADR — `adr-NNN-wasm-extension-loader.md` — should formalize the v2
decision after Prague go-live.

### Reliable shell auto-complete (every install mode)

> **Status:** 🟡 Partial — argcomplete-based bash/zsh completion ships
> today; tier-aware filtering, cross-shell coverage (fish/PowerShell),
> auto-install on first run, and the `axi completions` lifecycle verb
> are 🟦 designed → Phase 2. See [§Status & Phasing](#status--phasing).

Auto-completion is a load-bearing piece of incremental revelation: when
the user types `axi <Tab>`, the shell suggests *only* the verbs they've
reached at their current tier and per-extension familiarity. This makes
the discoverable surface match the visible help surface, so the user
never tabs into a verb they're not yet meant to see.

Auto-complete must work, out of the box, in **every supported install
mode** — pip-installed venv, Homebrew, frozen binary, Docker, direnv-
managed workspaces, system-wide install, user-only install — and across
**every supported shell** — bash, zsh, fish, PowerShell.

#### Requirements

- **Shell completion scripts ship as part of the install.** No separate
  download, no manual configuration step in the happy path. The first
  `axi` invocation after install detects the user's shell and either
  installs the completion script in the right location (with the user's
  consent) or prints the one-liner the user can paste.
- **Completions are dynamic.** When an extension is installed, removed,
  or upgraded, completion definitions refresh automatically. This is the
  same `axi update` hook that refreshes cross-harness slash-command
  shims (see `prd-commands-generator.md`).
- **Completions respect the tier model.** A user at `core` tier sees
  only `starter + core` verbs in tab-suggestions. `axi help --all` plus
  the explicit `axi <ext> <Tab>` form remain available when the user
  knows what they're looking for.
- **Per-verb argument completion.** Where a verb takes a positional
  whose values are enumerable (e.g. `axi nodes show <name>`,
  `axi classroom show <course>`), the
  completion script consults the live state to suggest valid values.
- **Graceful degradation in restricted shells.** Where dynamic
  completion isn't possible (frozen-binary install with no Python
  available at completion time), fall back to a static snapshot
  generated at install time and refreshed on `axi update`.

#### Verb

A dedicated CLI verb manages the completion lifecycle:

```bash
axi completions install [--shell bash|zsh|fish|powershell] [--scope user|system]
axi completions print   --shell bash       # emit the script for piping
axi completions refresh                    # called by axi update
axi completions uninstall
```

The `install` form is invoked automatically the first time `axi` runs
after install (with a one-time consent prompt), but the verb is also
available for users who want to wire completion manually or migrate
between shells.

### Helpful Errors

Errors include the *what*, the *why*, and a *next step*. The error
formatter is shared across the CLI so every command's failure mode
follows the same shape:

```bash
$ axiom model deploy broken.wasm
Error: Model validation failed

  × Missing required export: predict
  │
  │ The model must implement the axiom:surrogate/model interface.
  │ Required exports: predict, validate, get-metadata
  │
  help: Run `axiom model validate broken.wasm --verbose` for details
```

The "did you mean" suggestion in the *Smart, dynamic help* section
reuses this formatter; mistyped verb names produce the same shape with
a tier-aware suggestion in the help slot.

### Confirmation for Destructive Operations

Destructive verbs (anything that deletes, overwrites, or commits a
large workload) confirm before proceeding, with enough context to
make the consent meaningful:

```bash
$ axiom data backfill <large-table> --start 2020-01-01
⚠️  This will process 2.3 TB of data (~4 hours)

Continue? [y/N]
```

The threshold for "destructive" is set by the verb's manifest tier
(`advanced` and `internal` confirm by default; `core` confirms when
side-effects are user-visible) and can be sharpened per-verb.

---

## Dependencies

| Dependency | Purpose |
|------------|---------|
| LLM gateway | Agent intelligence (AXI, SCAN, TIDY, PRESS, TRIAGE, RIVET, CURIO) |
| Authentication service | OAuth2/OIDC integration |
| PostgreSQL + pgvector | State management, RAG embeddings |
| Pandoc | Document generation (PRESS) |
| Playwright | Browser-based OneDrive/Teams integration |
| WASM runtime | Local model validation |

---

## Open Questions

1. **Distribution**: Homebrew? apt? Standalone binary?
2. **Agent mode permissions**: Per-noun granularity (e.g., "agent can write to ops log but not compliance")? Or simpler role-based scoping via OpenFGA?
3. **Escalation UX in non-interactive contexts**: How do scripts/CI handle mode transitions? Likely: `--mode agent --yes` for pre-approved pipelines with explicit scope flags.
4. **Session persistence**: Should Plan mode drafts survive across sessions? Or are they ephemeral?

---

*For the complete command hierarchy, configuration schema, and implementation details, see the [CLI Technical Specification](../specs/spec-axi-cli.md).*
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
