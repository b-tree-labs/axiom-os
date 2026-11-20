# Teaching with AI — Modular AI Tutoring + Instructor Oversight

**Product lead:** Benjamin Booth
**Co-lead:** Ondrej Chvala (an institution, Senior Research Scientist, teaching laboratory)
**Status:** Refreshed 2026-04-30 against as-built state. Original PRD draft authored by Ondrej 2026-02-26 and folded into this product line under Ben's product leadership 2026-04-30.
**Target cohort:** Prague summer 2026 (US-only Phase 1; classification boundary moves to Phase 2)

---

## 1) Elevator Pitch

A weekly, mastery-based course model that pairs LLM tutoring with instructor-led checkpoints to deliver scalable 1:1 learning while improving learning outcomes and reducing D/F/W rates.

## 2) Problem / Opportunity

- Students will use AI tools anyway; current course designs either ignore AI or treat it as a compliance problem.
- Large classes cannot provide consistent 1:1 tutoring; learning gaps compound silently until exams.
- Opportunity: use AI tutoring for daily practice and use instructors for high-value teaching — assessment, coaching, and targeted intervention — at scale.

## 3) Goals & Success Metrics

**Primary:** Increase mastery and course completion by combining AI-guided practice with human-verified checkpoints.

| KPI | Target | Status |
|---|---|---|
| Module mastery rate (pass checkpoint on first attempt) | +15% vs baseline | ❌ **Baseline not yet collected** — pre-cohort measurement plan needed before Prague start |
| DFW rate (D/F/Withdraw) | −20% vs baseline | ❌ Baseline not yet collected |
| Time-to-mastery (median days/module) | −10% | ❌ Baseline not yet collected |
| Instructor time per student (weekly) | ≤ baseline (no increase) | ❌ Baseline not yet collected |

**Open: KPI baselines.** *None of the targets above are measurable without a pre-cohort baseline.* Need to either (a) capture data from the prior offering of NE-101 / equivalent, or (b) accept that Prague summer 2026 IS the baseline and the comparison is against future cohorts.

## 4) Key Users / Personas

| Persona | Type | Need | Current support |
|---|---|---|---|
| **Students** | Primary | Practice, feedback, structure, clarity on acceptable AI use | ✓ `axi classroom join` / `ask` / `me` / quiz / threads — Tier A+B+C shipped |
| **Instructors / Lecturers** | Primary | Module objectives, monitoring, review-session prep, mastery assessment | ✓ `axi classroom prep` / `serve` / `briefs` / `brief` (CHALKE) / `evals` shipped |
| **TAs / Learning assistants** | Secondary | At-risk lists, intervention prompts, remediation flows | ⚠ **Gap** — instructor-side briefs exist; no TA-specific surface |
| **Academic leadership** | Secondary | Aggregate outcomes, equity, governance, scalability | ⚠ **Gap** — per-classroom surfaces exist; no cross-classroom aggregation yet |

## 5) Scope — Key Capabilities (MVP)

### 5.1 Weekly mastery module workflow

> Students receive objectives + tasks; completion tracked; instructor runs checkpoint and review.

| Acceptance criterion | Status | Notes |
|---|---|---|
| Instructor can publish module objectives/tasks | ⚠ Partial | `prep checkpoints` configures baseline/midpoint/final; "weekly module" structure isn't a distinct primitive |
| Students can view + complete within the week | ⚠ Partial | `axi classroom me` surfaces the brief; weekly task list isn't first-class |

**Gap:** explicit *weekly* module concept (vs. one-time checkpoints). Likely a `module` noun extending `checkpoints`.

### 5.2 LLM tutor with course-aligned guardrails

> Tutor follows course policy; supports practice without directly completing graded artifacts. Tone, citation expectations, "show work," allowed help.

| Acceptance criterion | Status | Notes |
|---|---|---|
| Tutor follows course policy | ✓ | System prompt set per classroom; RAG-grounded; refuses out-of-scope |
| Supports practice without completing graded artifacts | ❌ **Gap** | No explicit "this is graded; reduce help" surface; depends on instructor-set system prompt |
| "Show work" / chain-of-thought visible to student | ⚠ Partial | Qwen 3.5's `reasoning_content` is captured in LangFuse; not surfaced to student UI |
| Citation expectations enforced | ✓ | RAG-grounded answers cite `[Source N: file]` per Day 1 harness round 2+ |
| Per-student LLM-tier override (`dumb` / `standard` / `smart` / `smartest`) | ❌ **Gap** | Designed (`feedback_llm_tier_is_general_knowledge_dial`); not implemented (task #14) |
| Per-student RAG-mode override | ❌ **Gap** | Designed (task #12); not implemented |

### 5.3 Learning analytics agent

> Progress tracking + misconception detection + readiness summaries.

| Acceptance criterion | Status | Notes |
|---|---|---|
| Per-student summary (strengths, gaps, evidence snippets) | ✓ | CHALKE student briefs (Tier C) |
| Readiness status before review session | ⚠ Partial | Briefs exist; explicit "ready / not ready" gate isn't formalized |
| Misconception detection | ⚠ Partial | Q&A interaction logs surface confusion; no automated misconception classifier yet |
| Evidence excerpts + confidence signals | ⚠ Partial | LangFuse traces show evidence; instructor-facing "confidence" score not yet exposed |

### 5.4 Checkpoint assessment

> In-person quiz and/or short oral check with remediation path.

| Acceptance criterion | Status | Notes |
|---|---|---|
| Students can pass/redo with clear criteria | ✓ | `quiz` command supports retake |
| Instructor can assign remediation and re-check | ⚠ Partial | Threads + briefs support intervention; no explicit "remediation assignment" workflow |
| In-person / oral checkpoint integration | ❌ **Gap** | Only digital quiz today; no oral-check workflow |
| Broadcast-quiz-style synchronous assessment | ⚠ Partial | Tier C task C4 — *pending* |

## 6) Non-Functional / Constraints

| Requirement | Target | Current | Status |
|---|---|---|---|
| Tutor response p50 | < 3s | ~10-30s on Qwen 3.5 (reasoning model) | ❌ **Critical gap** |
| Tutor response p95 | < 8s | 30s+ regularly | ❌ **Critical gap** |
| Analytics summary generation | < 2 min/section | Untested at scale | ❓ **Unverified** |
| FERPA-aligned data handling | Required | Sovereign self-host on a self-hosted node helps; no formal consent UX yet | ⚠ Partial |
| Role-based access (student vs staff) | Required | Coordinator vs student roles exist; not formally enforced everywhere | ⚠ Partial |
| Encryption in transit + at rest | Required | TLS pending (LangFuse on NodePort, no ingress yet); at-rest = OS disk encryption | ⚠ Partial |
| Web-first + mobile-friendly | Required | **CLI-first today**; web `serve` exists but isn't student-facing UX | ⚠ **Architectural mismatch** with PRD |
| LMS Canvas integration | Desirable for MVP | `prep lms --canvas-course --fake` exists; real Canvas wiring TBD | ⚠ Partial |
| Auditable assessment records | Required | LangFuse provides per-AI-interaction audit; assessment records audit TBD | ⚠ Partial |
| Transparent AI-use policy | Required | Synthetic placeholder only (`fixtures-synthetic/03-policies.md`) | ❌ **Decision overdue** |
| Opt-out / alternative workflow | Required where institutional policy mandates | Not yet defined | ❌ **Gap** |

**Critical-gap consequences:**

- **Latency** is the single most user-visible defect. Three resolutions:
  1. Set `enable_thinking: false` for student-tutor calls (Qwen 3 supports it; cuts ~80% of latency)
  2. Use Bonsai 1.7B (Edge tier) for tutor mode; reserve Qwen 3.5 for instructor-side analysis
  3. Wire a non-reasoning Qwen variant
- **Web-first** vs CLI-first is the *biggest architectural delta from the PRD.* Current state is CLI-first per `project_prague_architecture_unresolved` ("CLI + federation is the product; browser optional secondary"). This is a deliberate divergence — the team should confirm the PRD reflects current intent or escalate.

## 7) Timeline (refreshed)

| Phase | Target | Status |
|---|---|---|
| Phase 0.1 — Pilot design + module templates + AI policy + minimal tutor | 2026-05-15 | ⚠ **2 weeks out**; AI policy + transcript logging decisions overdue (was 2026-04-15) |
| Phase 0.2 — MVP tracking + instructor dashboard + checkpoint workflow + limited pilot (1-2 courses) | 2026-08-31 | ⚠ Coincides with Prague summer cohort start (early June 2026) — **timeline tension** |
| GA / 1.0 — Expanded rollout + LMS integration + governance + reporting | 2027-01-15 | On schedule |

## 8) Risks & Open Questions (status as of 2026-04-30)

| Risk | Current mitigation | Status |
|---|---|---|
| Overreliance on AI / shallow learning | Mastery checkpoints, citation requirements, refusal patterns | ⚠ Lacks oral micro-vivas + process-based rubrics |
| Privacy / data retention concerns | Sovereign self-host (a self-hosted node on K3S, LangFuse on-prem); no third-party data egress for instrumented paths | ⚠ Consent UX + retention policy not formalized |
| Instructor trust in analytics | LangFuse traces visible; CHALKE briefs include evidence | ⚠ "Confidence signal" surface not implemented |
| Equity gap from unequal AI access | Institution-provided sovereign access (self-hosted Qwen, no commercial-API requirement for students) | ✓ Architectural mitigation in place |

**Open questions:**

| Question | Original due | Status |
|---|---|---|
| What AI use is permitted for checkpoints and graded work? | 2026-04-15 | ❌ **Overdue 15 days** |
| What level of transcript logging is acceptable under campus policy? | 2026-04-15 | ❌ **Overdue 15 days** |
| Should readiness scoring be binary (ready/not) or rubric-based tiers? | 2026-05-01 | ⚠ **Due tomorrow** — current implementation is rubric-based by default |

## 9) Acceptance & Rollout

**Sign-off chain (per original):** course instructor, department curriculum lead, academic integrity office, privacy/compliance representative, instructional design lead.

**Rollout plan:**
- **Beta (pilot):** Prague summer 2026, 1 course (NE-101), one term, weekly feedback loop, US-only cohort
- **GA:** Expand to additional sections/courses; publish module library; staff training
- **Rollback criteria:** increased integrity incidents, significant negative outcome deltas, or unacceptable privacy findings

## 10) Contacts & Links

- **Product lead:** Benjamin Booth
- **Co-lead:** Ondrej Chvala (pedagogy + curriculum)
- **Reproducibility:** `axiom/docs/working/visual-journeys/day1-rag-harness/`, this PRD's path, `dont-split-the-table-draft.md` (paper #5)
- **LangFuse dashboard:** `http://example-host.example.org:30030` (org network; project `prague-cohort`)

---

## 11) Refreshed-state status snapshot — 2026-04-30

### Shipped (post-original-PRD)

- ✓ Tier A: end-to-end ceremony + Rich UI + dashboard + materials flow + 12-student E2E (2026-04-22/23)
- ✓ Tier B: Q&A engine, evals framework, baseline comparison, NE101 starter bank
- ✓ Tier C: learning modes (tutor/quiz/reflect/review), per-student briefs with curation, bidirectional threads, CI eval gate
- ✓ AEOS CLI complete (24 `axi ext` commands; 2026-04-22)
- ✓ Sovereign LangFuse self-host on a self-hosted node (K3S) (2026-04-30)
- ✓ Day 1 chunker fix: 67% → 98% on should-RAG-win battery (2026-04-30)
- ✓ Per-classroom RAG-mode policy (`prep rag --mode <mode>`)

### Critical gaps (must close before Prague start)

| # | Gap | Severity | Decision-owner | Default if undecided |
|---|---|---|---|---|
| **G1** | **AI-use policy for graded work** (overdue 15 days) | 🔴 **Blocker** | Ondrej + UT academic integrity | Conservative: tutor refuses to complete graded artifacts; explicit student-facing notice |
| **G2** | **Transcript logging / retention policy** (overdue 15 days) | 🔴 **Blocker** | Ondrej + privacy/compliance | Conservative: log to LangFuse, retain through term, anonymize aggregate at term end |
| **G3** | **Tutor latency** (PRD target p50<3s; current ~15s) | 🔴 **Blocker** for student UX | Ben | Set `enable_thinking: false` for tutor mode |
| **G4** | **KPI baselines** (no measurement plan) | 🟡 High | Ondrej | Either capture from prior NE-101 offering OR declare Prague 2026 as baseline |
| **G5** | **Readiness scoring shape** (due tomorrow) | 🟡 High | Ondrej | Rubric-based tiers (already implemented in CHALKE) |
| **G6** | **Per-student RAG + LLM tier override** (tasks #12, #14) | 🟡 High | Ben | Implement per-student `policy` command before Prague |
| **G7** | **Web-first vs CLI-first reconciliation** | 🟡 High | Ondrej + Ben | Confirm CLI is the product; doc the divergence from PRD §6 |
| **G8** | **AI-use policy SURFACED to student** (vs documented internally) | 🟢 Medium | Ben | Add `axi classroom policy` student-readable view |
| **G9** | **Oral-check / in-person quiz workflow** | 🟢 Medium | Ondrej | Defer to GA; pilot covers digital quizzes only |
| **G10** | **TA-specific surface** | 🟢 Low | Defer | Use instructor briefs; TA gets read-only access |

### Risk re-assessment

The most important risk *not* in the original PRD is **chunking-quality drift across re-ingestion of evolving lecture material.** Day 1 harness exposed that retrieval quality is dominated by chunking strategy. Without continuous chunking-quality monitoring across the term as Ondrej updates materials, retrieval can silently regress mid-cohort. *Recommendation: instrument the corpus-update path with regression tests against the canonical Q&A battery.*

### Open questions decision target — 2026-05-08 (1 week from now)

If **G1-G3** are not decided + implemented by 2026-05-08, the Prague-start window is at material risk. We need a stake-driving session.

---

## 12) Federation — How federation makes this PRD substantially stronger

The original PRD treats this as a single-institution product. Our architecture (ADR-022 through ADR-025) makes federation a first-class capability, and several of the hardest problems in this PRD are *uniquely* solved by federation.

### 12.1 Sovereign-by-design solves the privacy section

The PRD's §6 non-functional privacy requirements (FERPA-aligned, role-based access, data minimization, encryption) are downgraded from "risks to mitigate" to "architectural defaults" in a federated deployment:

- Student data **never leaves the local institutional node**. Only methodology, evals, curricula, and aggregate metrics flow across federation boundaries.
- Each institution keeps its own LangFuse, its own student-side index, its own coordinator.
- Commercial alternatives bolt on privacy compliance; federation has it as the default.

**Implication:** §6's privacy column becomes ✅ for any institution that joins the federation — privacy is supplied by the architecture, not by per-deployment compliance work.

### 12.2 KPI baselines become statistically meaningful

The PRD's success metrics (+15% mastery, −20% DFW, −10% time-to-mastery) are unanswerable in isolation because there is no measured baseline. Federation enables **multi-institutional concurrent measurement** — N universities running comparable mastery modules produce N baselines, large enough for confidence intervals and cross-institutional benchmarking.

**Implication:** the §3 success metrics become *publishable research claims* when measured federation-wide. This also strengthens **Paper #4 (KEP-LO — "The Tutor That Shows Its Work")** in the portfolio.

### 12.3 Module library — pedagogy itself federates

A *weekly mastery module* is, in federation terms, a **citable artifact**. Ondrej's Week 4 point-kinetics lecture (with prep state, RAG corpus pointer, eval bank, CHALKE configuration, and rubric) becomes a forkable / inheritable / attribution-cited unit that instructors at peer institutions can adopt, override, or improve.

- Currently: each classroom is standalone.
- Federated: modules compose across institutions; an OSU NE program can adopt UT-Prague Week 4 and contribute back its own variations.
- Authors retain attribution via multi-authority signatures.

**Implication:** §5.1 acceptance criteria expand — "instructor can publish module objectives/tasks" → "instructor can publish module objectives/tasks **as a federation-citable artifact**, optionally derived from a peer's published module."

### 12.4 Federated misconception detection

§5.3 of the PRD calls for *misconception detection*. Single-classroom detection has a sample-size problem — one cohort's noise drowns the signal. Federation provides **cross-institution misconception aggregation**: 100 universities running NE-101 = 100× the signal on which concepts students universally struggle with, without any individual student's data leaving any institution.

**Implication:** §5.3 becomes a federation primitive ("federated misconception detection") rather than a per-classroom feature.

### 12.5 Continuous pedagogical context for transfer students

Students moving between institutions (transfers, summer programs like Prague itself, dual-enrollment) currently encounter a fresh AI tutor at each. Federation enables **consent-based memory portability** — a Prague student carrying their UT learning history (with explicit opt-in) into the Prague-side tutor.

**Implication:** add to §4 (Personas) — *students-in-transit* as a meaningful sub-persona; add to §5 — *consent-based memory portability* as an MVP+1 capability.

### 12.6 Economy of scale for smaller institutions — the equity mitigation, scaled

The PRD's §8 *equity gap* risk is currently mitigated by "institution-provided access." Federation extends this: a community college can join and inherit self-hosted-tier Qwen + CHALKE quality without standing up its own infrastructure. The federation provides the LLM tier; the institution contributes its corpus + students + instructors.

**Implication:** §8 *equity* mitigation strengthens from "institution-provided access (per-institution)" to **"federation-provided access (institution-agnostic, scales to community-college and tribal-college tiers without per-institution infrastructure investment)."**

### 12.7 Cryptographically attestable pedagogical claims (for accreditation)

§6 calls for *auditable assessment records.* Federation's multi-authority signatures + trust graph (per ADR-028) mean pedagogical claims ("this module passes mastery at 92% across N institutions") are independently verifiable by accreditation bodies **without** requiring access to raw student data.

**Implication:** §9 sign-off chain expands — accreditation bodies become a verifiable-attestation consumer of federation-level claims, not an inspection-based reviewer.

### 12.8 Resilience — failover under load

A single-institution AI tutor has a single point of failure. Federation enables policy-controlled failover: if the self-hosted node is down, Prague students can route (with policy permission) to a peer node serving an equivalent LLM tier.

**Implication:** add to §6 non-functional — *availability* as a federation-derived capability.

### 12.9 Faster pedagogical iteration via LearnedSwitch

Federation applies the governance pattern from `project_learnedswitch_library_first` to pedagogy itself. New tutor prompts, eval rubrics, RAG policies are evaluated at peers *before* adoption — federated peer-review rather than each institution rediscovering the same bugs.

**Implication:** PRD becomes a *living* spec; federation supplies the maturation channel.

### 12.10 Implementation status of federation capabilities

| Federation capability | Status as of 2026-04-30 | Notes |
|---|---|---|
| Per-institution sovereignty (data residency) | ✓ Architectural default | Each self-hosted node operates standalone |
| `axiom://` URI scheme + cohort registry | ⚠ Stubs (ADR-016) | Per `project_federation_architecture` |
| A2A protocol | ⚠ Stubs | 7 scenarios passing in 70s test harness |
| Multi-authority signatures | ⚠ Spec | Spec drafted; runtime pending |
| Trust graph (EigenTrust-inspired) | ⚠ Stubs | Optimistic defaults (per ADR-028) |
| Federated misconception detection | ❌ **Not designed** | New work item |
| Module library (citable artifacts) | ❌ **Not designed** | New work item |
| Memory portability (consent-based) | ❌ **Not designed** | New work item |
| Federation-derived accreditation attestations | ❌ **Not designed** | New work item; tied to ADR-028 trust graph |

### 12.11 Federation roadmap for this PRD

| Phase | Federation capability | Tied to PRD release |
|---|---|---|
| **Prague summer 2026** (single-institution beta) | Sovereign deployment + per-classroom isolation (already shipped) | Phase 0.2 |
| **Post-Prague (Q4 2026)** | Module library + federated misconception detection (cross-institution comparable mastery metrics) | GA / 1.0 (Phase 1.0) |
| **Q1 2027** | Memory portability (transfer students), failover, accreditation attestations | GA + 1 (Phase 1.1) |

### 12.12 Why this matters for the §3 success metrics

The federation capabilities above transform §3 from "single-institution experiment" to **"reproducible cross-institution research program."** The +15% mastery and −20% DFW claims become statistically defensible across N institutions, not just observed at one. This is the difference between a UT-internal pilot and a *publishable, defensible, accreditation-relevant* educational AI methodology.

**Bottom line:** federation isn't an optional later feature for this PRD. It's the architectural answer to the privacy, scale, baseline, equity, attestation, and continuous-pedagogy problems already named in the PRD. The §3 metrics, the §6 privacy requirements, the §8 equity risk, and the §9 accreditation chain all *get easier* under federation — and several of them *only become possible* under federation.

