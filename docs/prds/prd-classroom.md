# Product Requirements: Classroom & Learning Module

**Product / Feature:** Axiom Classroom Module

**Owner:** Benjamin Booth  •  **Status:** Draft  •  **Last updated:** 2026-04-13

---

## Table of Contents
- [1) Elevator Pitch](#1-elevator-pitch)
- [2) Problem / Opportunity](#2-problem--opportunity)
- [3) Goals & Success Metrics](#3-goals--success-metrics)
- [4) Key Users / Personas](#4-key-users--personas)
- [5) Scope — Key Capabilities (MVP)](#5-scope--key-capabilities-mvp)
- [6) Non-Functional / Constraints](#6-non-functional--constraints)
- [7) Timeline](#7-timeline)
- [8) Risks & Open Questions](#8-risks--open-questions)
- [9) Acceptance & Rollout](#9-acceptance--rollout)
- [10) Contacts & Links](#10-contacts--links)

---

## 1) Elevator Pitch

A classroom overlay for Axiom that lets an instructor define a course in any discipline, enroll a known cohort, and then observe, measure, and publish findings on how AI-assisted learning affects student outcomes — while giving students a domain-grounded experience that outperforms raw ChatGPT or Claude.

Students get domain-grounded, citation-backed answers with cross-device continuity. Instructors get full observability, learning analytics, and structured research data. No other platform provides both.

Built for STEM first, but architecturally domain-agnostic. The same Course/Classroom infrastructure serves an engineering lab, a constitutional law seminar, a medical residency, or a language learning cohort — what changes is the corpus, system prompt, and eval suite, not the platform.

## 2) Problem / Opportunity

- STEM instructors increasingly want to integrate AI into their courses but have no way to observe, measure, or control what students do with generic chatbots.
- No classroom features exist in Axiom today: no student identity, no learning analytics, no instructor dashboards, no LLM tracing per-student, no structured assessment integration.
- Without instrumentation, researchers cannot publish credible papers on AI-in-STEM-education. The research paper is a primary deliverable for any adopting institution.
- Students need a friction-free entry point (web chat, not CLI installation) that still routes through curated domain knowledge via RAG.
- If the experience is worse than ChatGPT, instructors won't adopt. If it's measurably better — grounded answers, traceable learning, exportable research data — we have a publishable result and a reusable training module.
- **First deployment:** A domain course with a small pilot cohort (summer 2026, 12 students). Domain-specific extensions (e.g., a physics corpus, simulation input generation) are provided by the consumer layer, not by this module.

## 2.5) Design Principles

### Enter through the end

**Principle:** A new user's first interaction is with a *working end
product* — a live classroom, a graded quiz, a signal brief — not with
configuration, manifest authoring, or admin setup. The preparation
flow runs *last*, derived from what the end product revealed the user
actually wanted.

**What this rules out:**

- "Fill in this YAML and then we can start." Instructors should never
  hand-author manifests. Syllabus text goes in; a populated manifest
  comes out (see [#53 syllabus extraction](../specs/spec-classroom.md#syllabus-extraction)).
- "Create a course, then a classroom, then enroll students, then…"
  Users should experience a seeded demo classroom immediately and
  only touch creation flows when they want to depart from the seed.
- "Configure the LLM provider before your first turn." The installer
  ships with a stub provider and a one-click upgrade; the first chat
  works before any keys are entered.
- "Set access tiers and classification up front." Defaults (public,
  unclassified) are correct for 95% of content; escalation happens at
  retrieval-audit time, not ingest-time.
- "Register your MCP server config." `axi ext mcp` generates it from
  the extension manifest (see [#29](../adrs/adr-029-federation-composition.md)).

**What this enables:**

- Demo-first onboarding: `axi classroom demo` spins a pre-seeded
  cohort and runs one quiz-score-brief cycle in under 60 seconds so
  the skeptical instructor sees the payoff before touching config.
- Syllabus-driven prep: the instructor drops a PDF or markdown
  syllabus and the prep checklist lights up with extracted LOs,
  assessments, and schedule — nothing to invent.
- Auto-generated admin surfaces: MCP config, CLI completion, tmux
  status-line, and IDE integrations come from manifests the user
  never has to write.

**How this shapes every feature PR:** If a feature requires a user
to do configuration before seeing value, it doesn't enter through the
end — rework it until the first meaningful interaction is with output,
not input.

This principle is load-bearing for Prong 2 ("integrate with existing
workflows with near-zero friction") of §10 Adoption Strategy, and
cross-referenced from [spec-classroom.md §0.2](../specs/spec-classroom.md).

## 3) Goals & Success Metrics

- **Primary goal:** Deliver a measurable, instrumented STEM learning experience that outperforms generic LLMs on domain accuracy and learning outcomes, and produces a publishable dataset.
- Success metrics:
  - 100% of student interactions are traced, attributed, and classifiable.
  - Pre/mid/post quiz scores show statistically significant improvement (paired t-test, p < 0.05).
  - NPS >= 40 from student exit survey.
  - Instructor can view per-student and cohort-level dashboards without writing queries.
  - Paper submitted to a peer-reviewed venue within 6 months of course completion.

## 4) Key Users / Personas

- **Instructor:** Defines course goals and syllabus. Monitors student engagement and comprehension. Intervenes when a student is stuck or heading down a misconception path. Exports data for research. Varying technical comfort — must not require CLI fluency.
- **Student (STEM learner):** Uses Axiom Chat to ask questions, generate calculations, explore domain concepts. Varying technical levels. Expects a web-based chat that works on laptop/phone. Zero tolerance for install friction.
- **Researcher:** Needs clean, exportable data: per-student traces, session logs, quiz scores, survey responses, chat classifications. Needs reproducible analysis.

## 5) Scope — Key Capabilities (MVP)

### 5.1 LLM Tracing & Observability

1. **Per-turn trace records** — Every LLM call captures: `trace_id`, `session_id`, `student_id`, `timestamp`, `prompt_tokens`, `completion_tokens`, `model`, `latency_ms`, `rag_chunks_retrieved`, `rag_relevance_scores`, `tool_calls[]`, `cost_usd`. Stored in structured JSONL + queryable via SQL.
2. **Trace dashboard** — Instructor-facing view: total turns, tokens, cost, latency distribution, error rate, per-student breakdown. Exportable to CSV/Parquet.
3. **Session replay** — Instructor can read any student's full conversation (with student consent per IRB/GDPR).

### 5.2 Student Identity & Learning Logs

> **Auth staging (decided 2026-04-16):** token auth for MVP (Tier 0);
> OIDC adapter is first post-Prague work (Tier 1); InCommon/eduGAIN
> at scale (Tier 2); PIV/CAC for gov (Tier 3). Each tier is
> independently shippable. See `project_auth_tier_staging` memory.

4. **Cohort enrollment** — `axi classroom create --name "STEM Course 2026" --students students.yaml` where `students.yaml` lists `{name, email, student_id, nationality}`. Each student gets a unique token for web chat authentication. **Nationality** is a signed instructor attestation at enrollment (separate from the auth token) used by the export-control per-access gate (§5.11.4). For Tier 1+: nationality flows as an OIDC claim from the institutional IdP.
5. **Learning log per student** — Append-only ledger of every interaction, tagged with session context. Auto-classified (see 5.4). Instructor can annotate entries.
6. **Student profile** — Aggregated view: total sessions, total turns, topics covered (from RAG chunk metadata), misconceptions flagged, quiz scores.

**Auth tiers (staged, each independently valuable):**

| Tier | Mechanism | When | Unblocks |
|------|-----------|------|----------|
| 0 | Instructor-issued tokens (TTL'd, per-student) | MVP / Prague | 12-student bounded cohort, mixed nationality |
| 1 | OIDC adapter (Kratos + per-federation IdP list) | First post-Prague | Multi-semester, 50+ students, UT SSO + partner IdP fallback |
| 2 | InCommon/eduGAIN (SAML→OIDC bridge) | Scale deployment | Cross-institutional at thousands of users; EU students auth via home university |
| 3 | PIV/CAC/consortium IdP (X.509 client cert or FICAM OIDC) | Gov/industry partnerships | DoE/DoD collaborators |

**Prague contingency for mixed US + EU cohort:**
- Both UT USA students and non-UT EU students receive tokens
  from the instructor at enrollment.
- Nationality attestation: instructor signs `nationality: US`
  or `nationality: CZ` (etc.) per student at enrollment time.
- Export-controlled modules: deterministic per-access gate
  checks nationality against content EC stamp. US students
  see all modules; EU students see only non-EC modules (per
  §5.11.4).
- GDPR consent: separate accept-terms flow at first login.
  Consent record attached to student identity.
- If EU data-residency is required: Prague-hosted node stores
  EU student data locally; LangFuse traces route to local or
  a self-hosted node (with consent). See §5.11 federation integration.

### 5.3 Course vs. Classroom Model

A **Course** is a reusable template — the syllabus, learning objectives, assessment schedule, corpus references, questionnaire manifests, and system prompt template. It lives in version control, can be versioned, forked, and shared across institutions and semesters.

A **Classroom** is a live instance of a Course — a specific cohort of students, a date range, active sessions, real-time traces, and enrollment state. It has a lifecycle: `provisioning → active → completed → archived`. One Course can spawn many Classrooms.

7. **Course definition** — YAML manifest (`course.yaml`) defining: course title, version, learning objectives (tagged with keywords), topic schedule (week-by-week), reference materials (mapped to RAG corpus/knowledge packs), assessment schedule, questionnaire manifests (begin/end interviews), domain system prompt template. Managed via the same `ArtifactRegistry` infrastructure as Model Corral — schema validation, semver versioning, `StatusMachine` lifecycle (draft→review→published→deprecated→archived), `.axiompack` distribution, 3-tier discovery, federation sharing. `axi course create`, `axi course publish`, `axi course share`.
8. **Classroom instantiation** — `axi classroom create --course ne-fundamentals-v1 --students students.yaml --start 2026-06-15`. Creates a Classroom from a Course + cohort + date range. Provisions student accounts in Open WebUI, distributes knowledge packs, registers questionnaires.
9. **Objective tracking** — Each learning objective has a rubric (what constitutes demonstrated understanding). The system maps student questions/responses to objectives via embedding similarity. Dashboard shows per-student coverage of objectives.
10. **Instructor alerts** — SCAN Signal integration: detects when a student has been stuck on the same topic for >N turns without resolution, or when a student's questions reveal a misconception pattern. Surfaces as a Signal to the instructor.

### 5.4 Interaction Metrics & Classification

11. **Multi-label classification of interactions** — Classification is applied **per-turn**, not per-session. A single session typically spans multiple categories as the student's intent evolves. Each turn receives one or more labels:
    - **Q&A** — Student asks a factual question, gets an answer.
    - **Generative** — Student asks the LLM to produce something (input file, calculation, plot, LaTeX). Sub-labeled by artifact type.
    - **Exploratory** — Open-ended "what if" / conceptual exploration.
    - **Debugging** — Student is troubleshooting an error or result.
    - **Metacognitive** — Student reflects on their own understanding ("I don't think I understand why...").
    - **Fun/Off-topic** — Non-course interaction.
    A session's profile is a **label distribution** — e.g., "45% Q&A, 30% Generative, 20% Exploratory, 5% Fun" — not a single category. This distribution is the research-useful metric: sessions that shift from Q&A-heavy to Exploratory-heavy over a semester may indicate deepening engagement. Classification via lightweight LLM call per-turn (batch, not real-time).

12. **Quantitative metrics per student:**
    - Sessions count, total turns, avg turns/session, avg session duration
    - Token consumption (prompt vs. completion)
    - RAG hit rate (% of turns that retrieved relevant chunks)
    - Topic distribution (which learning objectives touched)
    - Label distribution per session and aggregate (% Q&A, % Generative, etc.) — tracked over time to show evolution
    - Time-on-task estimates

13. **Qualitative instruments:**
    - Pre-course quiz (pencil-and-paper, baseline domain knowledge)
    - Mid-course quiz (same format, measures growth)
    - Post-course quiz (same format, final measurement)
    - NPS survey (1-10 scale + free text)
    - **Begin-of-course interview** — conducted via Axiom's structured Q&A (see 5.5). Captures: prior AI experience, domain background, learning expectations, comfort level with AI tools.
    - **End-of-course interview** — same mechanism. Captures: usefulness, trust, learning quality, comparison to other tools, what surprised them, what they'd change. Paired with the begin interview for before/after narrative analysis.

### 5.5 Structured Q&A Workflow

14. **Static Q&A engine** — A generalizable, interview-style workflow within the chat interface (comparable to Claude Cowork's guided conversation experience). The instructor defines a **questionnaire manifest** (YAML) with ordered questions, optional branching logic, and response type constraints. The system presents questions one at a time in a conversational tone, collects responses, and stores them as structured data — not free-form chat logs.

    Key properties:
    - **Deterministic question order** — not LLM-generated. The LLM provides conversational framing and follow-up probes, but the core questions are fixed and identical for every participant (critical for research validity).
    - **Response typing** — each question specifies expected response type: `free_text`, `likert_scale(1-5)`, `multiple_choice(options)`, `numeric`, `yes_no`. The LLM validates and gently re-prompts if the response doesn't match.
    - **Branching** — optional conditional questions (e.g., "If you answered 'yes' to Q3, ask Q3a").
    - **Completion tracking** — dashboard shows which students have completed which questionnaires.
    - **Export** — responses export as structured CSV/Parquet with one row per student per question, not as raw chat transcripts.
    - **Reusable beyond classrooms** — this is an Axiom-level primitive. Useful for onboarding interviews, feedback collection, intake forms, user research. Any context where you want structured data gathered through a conversational interface.

15. **Questionnaire manifest example:**
    ```yaml
    id: begin-of-course-interview
    title: "Pre-Course Interview"
    description: "Baseline assessment of student background and expectations"
    tone: conversational    # LLM wraps questions in natural language
    allow_probes: true      # LLM may ask one follow-up probe per question
    questions:
      - id: Q1
        text: "How would you describe your experience with AI tools like ChatGPT or Claude?"
        type: free_text
      - id: Q2
        text: "How comfortable are you using AI to help you learn technical subjects?"
        type: likert_scale
        scale: [1, 5]
        anchors: ["Not at all comfortable", "Very comfortable"]
      - id: Q3
        text: "Have you used AI tools in a previous course?"
        type: yes_no
      - id: Q3a
        text: "What did you use it for, and how helpful was it?"
        type: free_text
        condition: "Q3 == yes"
      - id: Q4
        text: "What do you hope to get out of using an AI assistant in this course?"
        type: free_text
    ```

### 5.6 Student Learning Toolkit & Onboarding

Students should not just be handed a chatbot — they should be taught a set of **AI-powered learning tools** as foundational skills at the outset of the Classroom. This is itself a learning objective: students learn *how to learn* with AI, not just *how to ask questions*. The onboarding workflow (WF-2) includes guided training on each tool.

**Core learning tools students should be familiar with before coursework begins:**

1. **Chat (Q&A and exploration)** — The primary interface. Students learn how to ask effective questions, how to evaluate answers critically, and how to recognize when the AI is citing sources vs. generating from training data. Onboarding includes a guided "first conversation" that demonstrates good prompting, shows how citations work, and teaches the student to verify claims.

2. **Iterative Research (CURIO)** — The most important tool students may not know they need — and the one that distinguishes this platform from every other AI learning tool. This is not "ask a question, get a report." This is a **Karpathy-style iterative research loop** (see `docs/working/curio-research-loop.md`) where the student launches an investigation that runs multiple iterations — formulating hypotheses, searching across corpora, evaluating evidence, surfacing contradictions, refining understanding, and converging on a synthesis with full provenance.

   **Why iterative research is a learning tool, not just a productivity tool:**
   - The loop discovers questions the student didn't know to ask. Iteration 1 searches for "accident-tolerant fuels." Iteration 2 discovers the real question is "what are the neutronics penalties of FeCrAl cladding?" — a question the student would never have formulated on their own.
   - Contradictions drive the next iteration, not consensus. When CURIO finds two sources that disagree, the disagreement becomes the input to the next cycle. Students learn that knowledge is contested, not settled — and that investigating contradictions is how experts actually work.
   - The process is auditable. Every iteration is logged: what hypothesis was tested, what evidence was found, what changed, why. The student can trace the evolution of understanding, not just read a final report. The instructor can see *how* the student's thinking developed.
   - The loop is resumable. A student starts researching SMR safety on Monday, picks it up Wednesday with new context from Tuesday's lecture, and converges by Friday. Research is a process, not an event.
   - The loop state is the deliverable. For research assignments, students submit their loop state — the evolution of their investigation — not just the final synthesis. This is pedagogically richer than a paper: it shows the path, not just the destination.

   **What students learn to use iterative research for:**
   - Literature review for assignments and projects (multi-iteration, not single-shot)
   - Deep dives into topics the lecture only surveyed
   - Investigating contradictions between sources (CURIO surfaces these explicitly)
   - Building annotated bibliographies with provenance chains
   - Developing research methodology — the loop teaches the *process* of systematic investigation

   **Federation multiplier (Mode 1: independent with sharing):** When multiple students run research loops on related topics, their findings cross-pollinate via classroom federation. Student A's discovery about regulatory pathways informs Student B's economic viability analysis. The cohort converges faster than any individual. This is the collaborative research primitive that makes the classroom more than the sum of its parts.

   **Onboarding exercise:** Each student launches one research loop on a topic of their choice during onboarding (`/research <topic>` or via chat). They watch it run for 2-3 iterations, then: (a) examine the hypothesis evolution across iterations, (b) identify one contradiction CURIO surfaced and assess whether the resolution is sound, (c) verify one source citation against the original document, and (d) submit a brief reflection comparing what the loop discovered vs. what they expected. This teaches three habits from day one: source verification, contradiction awareness, and the understanding that research is iterative, not one-shot.

3. **Structured Q&A (interviews/assessments)** — Students experience this during the begin-of-course interview. They learn that some interactions are structured (fixed questions, typed responses) vs. free-form chat. This prepares them for in-platform assessments and check-ins.

4. **Session management** — Students learn to create named sessions for different purposes ("Homework 3", "Lab prep", "Exam review") rather than dumping everything into one long conversation. Good session hygiene makes their own learning history more useful and makes the research data cleaner.

5. **Signal awareness** — Students learn that the system monitors engagement patterns and can detect when they're stuck. They learn how to issue a formal help request (`/help-request`) rather than silently struggling. De-stigmatize asking for help by making it a first-class tool.

6. **Publish (PRESS)** — For courses with writing components, students learn to use the publish workflow to draft, review, and submit reports. The AI assists with writing but the student is the author. Teaches responsible AI-assisted writing.

7. **Model Corral (domain-specific)** — For courses that involve simulation codes, students learn to discover, validate, and use registered models. This is consumer-layer-specific (a domain consumer provides it for its discipline; other domains would have their own).

**Onboarding sequence (delivered via AXI during WF-2):**

| Step | Tool | Activity | Completion Criteria |
|------|------|----------|-------------------|
| 1 | Chat | Guided first conversation: ask a domain question, examine the citations, verify one claim | Student submits verification |
| 2 | Iterative Research | Launch a research loop (`/research`), watch 2-3 iterations, examine hypothesis evolution, identify one contradiction, verify one citation, submit reflection | Loop state saved + reflection submitted |
| 3 | Session Mgmt | Create a named session, demonstrate switching between sessions | ≥2 named sessions exist |
| 4 | Structured Q&A | Complete the begin-of-course interview | Interview marked complete |
| 5 | Help Request | Practice issuing a help request (instructor acknowledges) | Help ticket created and resolved |
| 6 | Publish | (Optional, if course includes writing) Draft a short reflection on onboarding | Document generated |

Each step is tracked in the student's onboarding checklist. AXI guides the student through conversationally — "Now let's try iterative research. Pick a topic you're curious about in this course and say `/research <topic>`." The instructor sees completion status per student in the dashboard.

**Why this matters for research:** If we can show that students who completed the full onboarding toolkit used the system more effectively (better session hygiene, more iterative research usage, fewer retrieval misses, higher quiz scores) than students who skipped steps, that's a publishable finding about AI literacy's impact on learning outcomes. The iterative research tool specifically enables a second research question: do students who run more research iterations produce higher-quality syntheses? Does the loop's contradiction-surfacing lead to deeper understanding (measurable via pre/post quiz delta on topics where contradictions were found)?

### 5.7 Chat Interface — Open WebUI

Rather than building a chat UI from scratch, we deploy **Open WebUI** (MIT license, 124K+ GitHub stars) as the student-facing chat layer. Open WebUI already provides:

16. **Web chat UI** — Full-featured, mobile-responsive chat interface with markdown rendering, code highlighting, streaming, and Mermaid diagram support. Themeable with custom branding.
17. **Cross-device session continuity** — Sessions are server-authoritative (stored in PostgreSQL). A student can start a conversation on their laptop in the classroom, walk to a lab site, pull out their phone, and continue exactly where they left off. Multiple named conversations per student (e.g., "Homework 3", "Lab prep"), switchable from any device.
18. **Multi-user auth with RBAC** — Admin (instructor) and user (student) roles. Accounts provisioned automatically during `axi classroom create`. OIDC/SSO available if institutional auth is needed.
19. **Built-in analytics** — Per-user token consumption, activity timeseries, model usage. Covers basic instructor visibility out of the box.
20. **OpenTelemetry export** — Wires into Langfuse for detailed LLM tracing.
21. **REST API** — Full programmatic access to all conversation data. Our classroom extension reads from this API for batch classification, objective mapping, and research export.

**Our Axiom backend is exposed as an OpenAI-compatible API endpoint.** Open WebUI connects to it like any other model provider. Every query routes through our RAG pipeline (course corpus + community corpus) with the Course-defined system prompt injected. Students see whatever the consumer layer brands it as their model — they never interact with raw OpenAI/Anthropic.

**CLI access** remains first-class for students who prefer a terminal: `neut chat --remote <server> --token <token>` connects to the same backend, same sessions (via our API, not through Open WebUI).

**What we build on top of Open WebUI:** The structured Q&A workflow (5.5), learning objective tracking (5.3), batch classification (5.4), SCAN alerts (5.3), AXI agent (5.8 workflows), CURIO autonomous research, and research export (5.10). These are Axiom extensions that read from the Open WebUI conversation store and Langfuse trace store — they don't modify Open WebUI itself.

**CURIO as iterative research engine:** CURIO is the Eval agent in the REPL framework — an autonomous research intelligence that runs Karpathy-style iterative loops: formulate hypotheses, search across corpora, evaluate evidence, surface contradictions, refine understanding, and converge on validated findings (see `docs/working/curio-research-loop.md`). In the classroom context, CURIO powers student research loops (multi-iteration literature review, contradiction investigation, annotated bibliographies), helps instructors build and iterate course corpora, and validates claims against source material. The loop state — the evolution of understanding across iterations — is itself a learning artifact and research data point. Grounding verification and retrieval quality metrics are handled by the eval framework (which invokes CURIO's primitives). This iterative, auditable approach is what makes the system *measurably more trustworthy* than generic LLMs — and that measurability is what makes the research paper possible.

### 5.8 Classroom Operational Workflows

These are the lifecycle workflows that make this a complete learning module. Each workflow is agent-driven — instructors and students interact via chat or structured Q&A, and the agents orchestrate the underlying operations. Where an existing agent's skills don't cover the workflow, new skills are specified.

Canvas (or any institutional LMS) remains the system of record for official enrollment and grades. Axiom's classroom module is the AI-augmented operational layer that sits alongside it.

---

#### WF-1: Course Enrollment & Syllabus Distribution

**Trigger:** Instructor creates a Classroom from a Course.
**Agent:** AXI (classroom agent) + PRESS (publisher)

1. Instructor runs `axi classroom create --course <id> --students students.yaml` or tells AXI via chat: "Set up the Prague NE class with these 12 students."
2. AXI provisions student accounts in Open WebUI (RBAC: user role).
3. AXI generates unique access URLs/credentials for each student.
4. PRESS generates a syllabus document from the Course manifest (markdown → DOCX/PDF) and publishes it to a shared location (OneDrive, Box, or in-app).
5. AXI sends enrollment confirmation to each student (email or message) with: access URL, syllabus, pre-course requirements checklist, begin-of-course interview link.
6. AXI registers the Classroom in the Canvas LMS via API (if configured) — or exports a CSV for manual Canvas import.
7. **State:** Classroom transitions from `provisioning → enrolled`.

**Canvas integration:** Axiom syncs roster from Canvas (pull) or pushes roster to Canvas (export). Canvas remains the grade book. Axiom is the AI interaction layer.

---

#### WF-2: Student Onboarding & Readiness Verification

**Trigger:** Student accesses their enrollment link.
**Agent:** AXI (Loop + Chat — a consumer layer may rebrand AXI)

1. Student opens their URL. AXI presents the **onboarding checklist** (defined in the Course manifest):
   - [ ] Read the syllabus
   - [ ] Complete the begin-of-course interview (structured Q&A)
   - [ ] Complete pre-course reading/exercises (if any)
   - [ ] Acknowledge data collection consent (IRB)
   - [ ] Take the baseline quiz (if administered via Axiom)
2. Each checklist item is tracked per-student. Some are self-reported ("I've read the syllabus"), some are system-verified (interview completed, quiz submitted).
3. AXI reports onboarding status to the instructor dashboard: green/yellow/red per student.
4. Instructor can review a student's readiness via chat: "Is Student 03 ready?" → AXI responds with their checklist status, interview highlights, and baseline quiz score.
5. Instructor explicitly marks each student as **verified ready** or **needs follow-up**. AXI will not allow a student to be marked ready if required items are incomplete.
6. **State:** Student transitions from `enrolled → onboarding → ready` (or `needs_followup`).

---

#### WF-3: Student Test-Taking

**Trigger:** Assessment date in the Course manifest, or instructor initiates manually.
**Agent:** AXI + Structured Q&A Engine

Two modes:

**Mode A: Pencil-and-paper (recommended for research validity)**
1. Instructor administers the quiz in person. No Axiom involvement during the test.
2. After the quiz, instructor imports scores: `axi classroom quiz import --file quiz1_scores.csv --assessment mid` or tells AXI: "Import the mid-course quiz scores from this file."
3. AXI validates the CSV, associates scores with students, and stores them in the Classroom's assessment record.

**Mode B: In-platform assessment (via structured Q&A)**
1. AXI activates the assessment questionnaire for the cohort at the scheduled time.
2. Students complete the quiz via the structured Q&A engine (fixed questions, response typing, no LLM assistance — the system prompt is explicitly set to "quiz mode" which disables RAG and helpfulness).
3. AXI auto-scores objective questions (multiple choice, numeric). Free-response questions are queued for instructor scoring (WF-4).
4. Time limits and submission deadlines are enforced by the Q&A engine.

---

#### WF-4: Instructor Test Scoring

**Trigger:** Quiz responses submitted (Mode B) or scores imported (Mode A).
**Agent:** AXI

1. For imported scores (Mode A): AXI stores scores and computes per-student and cohort statistics (mean, median, std dev, per-objective breakdown). Instructor sees results in the dashboard.
2. For in-platform quizzes (Mode B):
   - Objective questions are auto-scored immediately.
   - Free-response questions are presented to the instructor in a scoring queue. AXI presents each response with the rubric and an optional LLM-suggested score (instructor reviews and confirms/overrides).
   - Instructor can score via chat: "Show me unscored responses for the mid-quiz" → AXI presents them one at a time.
3. AXI computes aggregate analytics: score distributions, per-objective mastery rates, pre→mid→post trend lines.
4. If Canvas integration is configured, AXI pushes scores to the Canvas grade book via API.
5. **SCAN signal:** If a student's score drops significantly from pre to mid, SCAN generates a `student_struggling` signal for the instructor.

---

#### WF-5: Instructor-Student Check-ins

**Trigger:** Scheduled (weekly per Course manifest), or triggered by SCAN signal, or instructor-initiated.
**Agent:** AXI + SCAN

1. **Scheduled check-ins:** AXI sends each student a brief structured Q&A at the interval defined in the Course manifest (e.g., weekly):
   - "How are you feeling about the material this week?" (likert)
   - "What topic are you finding most challenging?" (free text)
   - "Is there anything you'd like more help with?" (free text)
2. **Signal-triggered check-ins:** When SCAN detects a `student_stuck`, `low_engagement`, or `misconception_detected` signal, AXI notifies the instructor and suggests a check-in. Instructor can:
   - Review the student's recent chat history (session replay via Open WebUI API)
   - Send a personalized message via the platform
   - Schedule a live conversation
3. **Instructor-initiated:** Instructor tells AXI (Neut): "How is Student 07 doing?" → AXI compiles a student briefing: recent activity, topics covered, quiz scores, check-in responses, any SCAN signals. Instructor can then reach out.
4. Check-in responses are stored as structured data and included in the research export.

---

#### WF-6: Student Formal Help Request / Remediation

**Trigger:** Student initiates via chat or dedicated command.
**Agent:** AXI

1. Student says "I need help" or "I'm stuck" or uses a formal command (`/help-request` in the chat). AXI distinguishes between:
   - **In-chat help:** AXI (as the chat agent) attempts to help directly via RAG + domain knowledge. Normal chat flow.
   - **Formal help request:** Student explicitly escalates beyond what the AI can provide. AXI creates a structured help ticket.
2. **Help ticket** captures: student ID, topic/objective, what they've tried (auto-summarized from recent chat), specific question, urgency (self-assessed).
3. AXI notifies the instructor (SCAN signal: `help_request`). Instructor sees the ticket in their dashboard.
4. Instructor responds via the platform (message to student) or schedules a live session.
5. **Remediation tracking:** If a student is flagged for remediation (by instructor), AXI creates a remediation plan:
   - Additional reading assignments (pushed via knowledge pack)
   - Targeted practice problems
   - Follow-up check-in schedule
   - Progress milestones
   AXI monitors the student's interactions against the remediation plan and reports progress to the instructor.
6. Help tickets and remediation plans are included in the research export (anonymized).

---

#### WF-7: Dropping a Student / Student Withdrawal

**Trigger:** Instructor or student initiates.
**Agent:** AXI

1. **Student-initiated withdrawal:**
   - Student requests withdrawal via chat or email.
   - AXI presents the end-of-course interview (abbreviated) as an exit interview to capture why they're leaving.
   - AXI deactivates the student's Open WebUI account (RBAC → disabled, not deleted).
   - All existing data (sessions, traces, quiz scores) is retained for research (with consent) but the student can no longer submit new interactions.
   - AXI notifies the instructor and updates the Classroom roster.

2. **Instructor-initiated removal:**
   - Instructor tells AXI: "Remove Student 04 from the class."
   - AXI confirms (human-in-the-loop — this is irreversible in the Classroom context).
   - Same deactivation flow as above.
   - AXI logs the reason (provided by instructor) in the Classroom audit log.

3. **Canvas sync:** If integrated, AXI updates the Canvas roster.
4. **Research data:** Withdrawn students' data is flagged `withdrawn` in the export. Researcher decides whether to include (intent-to-treat) or exclude (per-protocol).
5. **State:** Student transitions to `withdrawn`. Classroom cohort count updates.

---

#### WF-8: Replacing or Adding an Instructor / TA

**Trigger:** Administrative change.
**Agent:** AXI

1. Current instructor (or platform admin) tells AXI: "Add Dr. Jones as a co-instructor" or "Add Alice as a TA."
2. AXI provisions the new instructor/TA:
   - Creates an Open WebUI account with admin (instructor) or moderator (TA) role.
   - Grants access to the Classroom dashboard, student session replay, trace data, and scoring queue.
   - TAs get a scoped role: can view student interactions and score assessments, but cannot modify the Course manifest, drop students, or export research data.
3. **Instructor replacement:** If the lead instructor changes:
   - AXI transfers ownership of the Classroom.
   - Previous instructor can be downgraded to co-instructor or removed entirely.
   - All prior SCAN signals, check-in history, and scoring decisions are preserved.
4. AXI sends the new instructor/TA an onboarding brief: course overview, current week's objectives, cohort status summary, any active SCAN signals.
5. **Audit:** All role changes are logged in the Classroom audit log.

---

#### WF-9: Student Course Review & Instructor Evaluation

**Trigger:** Course completion (Classroom state → `completing`).
**Agent:** AXI + Structured Q&A Engine

1. When the Classroom reaches its end date (or instructor triggers completion), AXI initiates the end-of-course workflow for each student:
   - End-of-course interview (structured Q&A — see 5.5)
   - Post-course quiz (Mode A or B — see WF-3)
   - **Course review questionnaire:** structured Q&A capturing:
     - Overall course quality (likert 1-5)
     - Relevance of material to learning goals (likert)
     - Quality of course materials/RAG corpus (likert + free text)
     - What should be added/removed from the course? (free text)
   - **Instructor evaluation questionnaire:** separate structured Q&A:
     - Instructor responsiveness (likert)
     - Quality of check-ins and feedback (likert)
     - NPS: "How likely are you to recommend this instructor?" (0-10)
     - Free text: what did the instructor do well / what could improve?
   - **AI assistant evaluation:** separate structured Q&A:
     - How useful was the AI assistant for learning? (likert)
     - How much did you trust the AI's answers? (likert)
     - How did it compare to ChatGPT/Claude? (worse/same/better + free text)
     - Would you use it again in a future course? (yes/no + why)

2. AXI tracks completion of all end-of-course instruments per student. Sends reminders for incomplete items.
3. Instructor reviews aggregated results via dashboard. Course reviews and instructor evaluations are anonymized — the instructor sees aggregate scores and anonymized free-text responses.
4. **AI evaluation data** is NOT anonymized to the researcher (needed for per-student correlation with usage metrics and quiz scores), but IS anonymized in any published results.
5. All review/evaluation data is included in the research export.
6. **State:** Classroom transitions from `active → completing → completed`. Once all instruments are submitted and data is exported, instructor can archive: `completed → archived`.

---

#### WF-10: Student Work Submission & Grading

**Trigger:** Student completes an assignment and submits it for grading.
**Agent:** AXI + PRESS + CURIO

Assignments are defined in the Course manifest with due dates, rubrics, and submission type (document, calculation, code, model input file, etc.). The submission workflow is agent-driven end-to-end.

**Student submission flow:**

1. Student tells the chat: "Submit my homework 3" or uses `/submit homework-3`. AXI prompts the student to identify the work:
   - **From a session:** "Which conversation session contains this work?" → AXI extracts the relevant content from the named session (student's questions, AI-assisted calculations, generated artifacts).
   - **From a file:** Student uploads a document (PDF, DOCX, LaTeX, Jupyter notebook, input file) via the Open WebUI file upload or via CLI.
   - **From a session + file:** Combined — the session provides context, the file is the deliverable.

2. AXI validates the submission:
   - Is the assignment still open (before deadline)?
   - Does the submission meet minimum requirements defined in the rubric (e.g., minimum word count, required sections, expected file type)?
   - Are all required components present (e.g., "this assignment requires both a calculation and a written analysis")?
   If validation fails, AXI tells the student what's missing and allows resubmission.

3. **CURIO integrity check:** CURIO analyzes the submission for:
   - **AI attribution:** What fraction of the submitted work was generated by the AI vs. authored by the student? (Derivable from session history — CURIO can compare the submission against the AI's responses in the linked session.) This is not a plagiarism check — it's a transparency metric. The Course manifest defines the expected AI-use policy (e.g., "AI may assist with calculations but written analysis must be student-authored").
   - **Source verification:** Are claims in the submission supported by the course corpus? CURIO runs a grounding check against RAG.
   - **Completeness:** Does the submission address all rubric items?
   CURIO's analysis is attached to the submission as metadata — visible to the instructor during grading, not to the student.

4. PRESS generates a formatted version of the submission (if needed — e.g., markdown → PDF) and stores it in the Classroom's submission archive.

5. AXI confirms receipt to the student with a timestamp and submission ID. Submission is immutable after this point (student can resubmit before deadline, creating a new version; instructor sees all versions).

6. **State:** Submission transitions: `draft → submitted → grading → graded → returned`.

**Instructor grading flow:**

7. Instructor sees pending submissions in the grading queue (dashboard or via chat: "Show me ungraded homework 3 submissions").
8. AXI presents each submission with:
   - The student's deliverable (document/file)
   - Linked session history (what the student asked the AI, what they generated, how they iterated)
   - CURIO's integrity analysis (AI attribution ratio, grounding score, completeness)
   - The rubric with scoring fields
9. Instructor scores against the rubric. AXI offers an LLM-suggested score for each rubric item (instructor reviews and confirms/overrides — same pattern as WF-4).
10. Instructor adds written feedback (free text). AXI can suggest feedback based on the rubric gaps ("The submission doesn't address part 3c of the assignment — consider noting this").
11. AXI returns the graded submission to the student with score and feedback. Student sees their grade and can review the feedback in their chat.
12. If Canvas integration is configured, AXI pushes the grade.

**Late submissions:**
- Configurable in the Course manifest: `late_policy: {grace_period: 24h, penalty_per_day: 10%}` or `late_policy: reject`.
- AXI enforces automatically. Late submissions are accepted during the grace period with a penalty flag; after that, rejected with a message.

**Resubmission:**
- Configurable: `resubmission: {allowed: true, max_attempts: 3, deadline: same}`.
- AXI tracks version history. Instructor can see all versions and grade any or the latest.

---

#### WF-11: Student Presentation to Classroom

**Trigger:** Presentation assignment due date, or student/instructor initiates.
**Agent:** AXI + PRESS

Presentations are a form of publishing — the student's work is shared with the cohort, not just the instructor. This builds on PRESS's existing publish workflow but targets a classroom audience rather than an external endpoint.

**Pre-presentation:**

1. Student prepares their presentation using AI-assisted workflows:
   - Chat sessions for research and ideation
   - Iterative research (CURIO) for deep dives on their topic
   - PRESS for document/slide generation (markdown → presentation format)
2. Student submits their presentation materials via WF-10 (same submission workflow — the assignment type is `presentation`).
3. AXI validates the submission against presentation-specific rubric requirements (e.g., slide count, required sections like "methodology" and "conclusions", time estimate).

**Presentation delivery:**

4. When the presentation slot arrives (scheduled in the Course manifest or triggered by instructor), AXI transitions the presentation to `presenting` state and makes the materials available to the entire Classroom:
   - The presentation document/slides are published to a **Classroom-scoped shared space** — visible to all enrolled students and instructors via Open WebUI or a shared link.
   - The presenting student's relevant session history can optionally be shared (if the student consents and the Course manifest enables it) — showing the class how the student used AI to develop their work. This is pedagogically powerful: students learn from each other's AI-use patterns.

5. **Live Q&A support:** During or after the presentation, the cohort can ask questions. Two modes:
   - **In-person:** Presentation happens in the physical classroom. Students ask questions verbally. No platform involvement during the live session.
   - **Async/remote:** Students post questions in a dedicated presentation discussion session (auto-created by AXI). The presenting student responds. The instructor can moderate. This supports remote/hybrid classrooms and gives quieter students a channel to participate.

**Post-presentation:**

6. **Peer feedback:** AXI sends a brief structured Q&A to each non-presenting student:
   - "How would you rate this presentation?" (likert 1-5)
   - "What was the strongest aspect?" (free text)
   - "What could be improved?" (free text)
   Peer feedback is anonymized to the presenter. Instructor sees attributed feedback.

7. **Instructor grading:** Same as WF-10 grading flow, with the addition of peer feedback summary attached to the submission.

8. **Presentation archive:** PRESS archives the presentation materials, Q&A discussion, and peer feedback as a Classroom artifact. Available to all students for the remainder of the course (study material).

**Classroom knowledge contribution:**

9. **Best presentations enrich the corpus:** If the instructor marks a presentation as high-quality, CURIO can index the presentation materials into the course RAG corpus. Future students asking about that topic may retrieve content from a peer's presentation. The Course accumulates knowledge over semesters. In federation Topology D (cascaded), high-quality presentation artifacts can propagate upstream to the Course authority for inclusion in future versions.

---

#### Workflow-Agent Mapping Summary

| Workflow | Primary Agent | REPL Role | Supporting Agents | Key Skills Required |
|----------|--------------|-----------|-------------------|-------------------|
| WF-1: Enrollment & Syllabus | AXI (Neut) | Loop | PRESS | Open WebUI provisioning, Canvas API, document generation |
| WF-2: Onboarding & Readiness | AXI (Neut) | Loop + Chat | — | Checklist tracking, structured Q&A, readiness verification |
| WF-3: Test-Taking | AXI (Neut) | Loop | Q&A Engine | Quiz mode (RAG-disabled), time enforcement, score import |
| WF-4: Test Scoring | AXI (Neut) | Loop | SCAN | Auto-scoring, LLM-suggested scores, Canvas grade push, signal on score drop |
| WF-5: Check-ins | AXI (Neut) | Loop + Chat | SCAN | Scheduled Q&A, signal interpretation, student briefing |
| WF-6: Help/Remediation | AXI (Neut) | Chat + Loop | SCAN | Help ticket creation, remediation planning, progress monitoring |
| WF-7: Drop/Withdraw | AXI (Neut) | Loop | — | Account deactivation, exit interview, roster sync, data flagging |
| WF-8: Add/Replace Instructor | AXI (Neut) | Loop | — | RBAC provisioning, ownership transfer, onboarding brief |
| WF-9: Course Review | AXI (Neut) | Loop | Q&A Engine | Multi-instrument evaluation, anonymization, completion tracking |
| WF-10: Work Submission & Grading | AXI (Neut) | Loop | PRESS, CURIO (Eval) | Submission validation, rubric scoring, Canvas grade push |
| WF-11: Student Presentation | AXI (Neut) | Loop + Chat | PRESS, CURIO (Eval) | Classroom publishing, peer feedback, Q&A facilitation, corpus enrichment |
| Cross-cutting: Autonomous Research | CURIO | Eval | AXI, SCAN | Autoresearch, corpus construction, source validation, federated knowledge |

### MVP vs. Full Vision

**MVP (Prague summer 2026, 12 students):** Sections 5.1-5.5, 5.7 (Open WebUI), WF-1 through WF-6, WF-10. Canvas integration is manual CSV export, not API. Federation is hub-and-spoke only (Topology A).

**Post-MVP (fall 2026+):** WF-7 through WF-9, WF-11, Canvas API integration, federation Topologies B-D, per-student adaptive chunking, iterative research onboarding exercise, presentation peer feedback, ArtifactRegistry extraction from Model Corral.

### 5.9 Federation for Classroom

22. **Classroom as a federation topology** — The instructor's node is the coordinator. Each student's web session is a lightweight "leaf node" in federation terms. This gives us:
    - Centralized knowledge corpus (instructor publishes, students consume)
    - Per-student session isolation (no cross-student data leakage)
    - Aggregated analytics at the coordinator
    - A real-world federation deployment we can cite in papers
23. **Knowledge pack distribution** — Instructor publishes a `.axiompack` containing course materials. Students' sessions automatically load it. Mid-course updates propagate without student action.

### 5.9.5 Classroom Archive & Learning Harvest

Classrooms have a full lifecycle that ends in archive and (optionally) purge. The archive process harvests learnings for participants to take with them and proposes valuable findings for promotion into RAG corpora.

#### Archive Lifecycle

Classroom lifecycle: `provisioning → enrolled → active → completing → completed → archived → purged`.

| Transition | Trigger | Default Timing |
|------------|---------|----------------|
| `active → completing` | End date in Course manifest | Automatic on date |
| `completing → completed` | All end-of-course instruments submitted (or instructor override) | Automatic when complete |
| `completed → archived` | Grace period elapsed | **90 days after `completed`** (instructor can override to earlier) |
| `archived → purged` | Long-term retention limit | **2 years after `archived`** — prompted, never automatic |

Rationale:
- **90 days completed → archived:** Allows time for grade appeals, paper analysis, and end-of-semester settling. Classroom is read-only but accessible.
- **2 years before purge prompt:** Research data may be needed for paper revisions or longitudinal analysis. Purge is always an explicit instructor decision, never automatic.

Instructor can manually trigger at any time: `axi classroom archive <id>` or `axi classroom delete <id>` (delete requires prior archive).

#### Learning Harvest for Participants

When a classroom transitions to `archived`, each participant receives a **harvest bundle** (`.axiompack` format) containing everything they authored or contributed, fully attributed:

**Student harvest bundle:**
```
my-harvest-prague-ne-2026.axiompack/
├── research/
│   ├── loops/           # Research loops I ran, with full state and iteration history
│   └── findings/        # Validated findings I contributed (with attribution chain)
├── sessions/
│   ├── named/           # Named chat sessions (HW3, Lab prep, etc.)
│   └── submissions/     # Assignments I submitted + grading feedback
├── presentations/       # Anything I presented to the class
├── interviews/          # My own interview responses (not peer data)
├── citations/           # Sources I verified during the course
├── cohort-aggregate/    # Anonymized cohort metrics (if opted in via consent)
└── MANIFEST.yaml        # Provenance, consent receipts, contents
```

The harvest bundle is portable and loadable into the student's next Axiom node. When they take a new course, they can opt in to reference their harvest bundle as prior knowledge for their CURIO. Learning compounds across courses, across their career.

**Instructor harvest bundle:** Everything they created (course manifest versions, assessments, rubrics, grading decisions) plus cohort-level anonymized research data. This feeds the next semester's Course template.

#### Knowledge Promotion to RAG

Valuable findings from a classroom should not die with the classroom. A promotion ladder moves findings from narrower scopes up into broader ones, while broader scopes inherit down into narrower ones:

```
Community RAG (federated institutions — broadest)
    │   ↑ promote (community consensus; CURIO proposes; eval gate + review)
    │
Organization RAG (all courses at this institution)
    │   ↑ promote (org admin approves; instructor proposes)
    │
Course Template RAG (all future instances of this Course)
    │   ↑ promote (instructor approves; CURIO proposes; eval gate)
    │
Course RAG (this classroom's corpus)
    │   ↑ promote (instructor approves; CURIO proposes)
    │
Student's Personal RAG (individual — narrowest)
    ↑ auto (student-authored)
```

**Read direction (inheritance):** broader tiers flow down — a student automatically has access to Community + Org + Course Template + Course + Personal content when searching.

**Write direction (promotion):** findings move up through approval gates. Each rung requires higher confidence, broader eval validation, and more approvers. A finding useful in one student's context may not generalize to the community.

**What CURIO proposes for promotion:**

- **Resolved contradictions** — student investigated conflicting sources, reached a defensible resolution, confirmed by peer or instructor
- **High-confidence research findings** — loop converged with confidence ≥0.85, diverse sources, no unresolved contradictions
- **Excellent presentations** — marked high-quality by instructor during grading
- **Novel syntheses** — cross-sub-question connections that weren't in the original corpus
- **Validated source corrections** — student flagged an error, CURIO verified, instructor confirmed
- **Cohort patterns** — multiple students struggled with the same concept → course template gap, proposed for corpus improvement next semester

**Approval flow (default):** CURIO proposes, instructor approves per-item.

```
CURIO: 3 promotion candidates from Week 2:
  1. Student 07's FeCrAl creep resolution — high confidence, 
     cross-references 4 sources, fills a known gap in the course 
     corpus. → Course RAG?
  2. Student 03's presentation on NRC licensing pathways — marked 
     high-quality by you during grading. → Course RAG with 
     attribution to Student 03?
  3. Cohort pattern: 7/12 students struggled with the four-factor 
     formula. Suggests corpus gap. → Flag for Course template 
     update?

Approve all? [y/N/select]
```

**RACI delegation:** Instructor can grant CURIO autonomy for low-stakes promotions via natural-language policy broadcasting (see `docs/working/natural-language-policy-broadcasting.md`):

```
Instructor: @curio you may auto-promote findings to the course 
            RAG if confidence ≥0.9 and they pass the eval gate. 
            Notify me weekly with a summary. Keep proposing 
            Course template and org-level promotions for my review.
```

This becomes an RACI grant: `finding_promotion(course_rag, confidence≥0.9)` → `R=CURIO, I=Instructor`. Higher-stakes promotions (Course template, org RAG, community RAG) always require human approval.

**Attribution is permanent.** Every promoted artifact carries its full attribution chain forever:

```yaml
finding_id: f-fecral-creep-resolution
promoted_to: [course_rag/prague-ne-2026, course_template/ne-fundamentals-v2]
attribution:
  original_contributor: student_07
  course: prague-ne-2026
  research_loop: rl-student07-atf-materials
  verification:
    - peer: student_04 (confirmed via independent research)
    - instructor: ondrej_chvala (approved promotion)
    - eval: passed domain-accuracy suite (2026-07-15)
```

Students can cite their own contributions in future work. Instructors can show "X students contributed findings that made it into the course template."

#### The Compound Knowledge Flywheel

This is the data → fact → pattern → knowledge cycle at classroom scale:

- **Semester 1** students fill gaps in the course corpus through research loops
- **Semester 2** starts from a richer corpus; students push deeper; find new gaps
- **Semester 3** starts richer still
- **After 3-4 semesters** the course has knowledge no single instructor could have curated

At org/community scale, the same flywheel runs across institutions. UT, INL, OSU contribute findings from their student cohorts through federation. The community RAG gets smarter every semester, everywhere, with every contribution traceable to its original human author.

### 5.9.6 Alumni & Longitudinal Identity

A classroom is an event; a Course template is persistent; **alumni are the living bridge between them**. When a student completes a Classroom, they can opt in to a persistent alumnus identity that preserves their contributions with permanent attribution, optionally hosts a scope-limited CURIO instance for future students to query, and enables consented longitudinal research participation.

Alumni records live at the **Course template level** (not the classroom instance), so they survive classroom archive. Ownership model: **the alum owns their data**, not the institution. Course templates accumulate not just corpus content across semesters, but a federated network of people who've gone through them.

This is the feature that makes longitudinal educational research feasible at scale — most such studies die for lack of follow-up, but alumni-as-federation-identity makes subjects findable and opt-in reachable indefinitely.

Full design: see **`docs/prds/prd-alumni.md`**.

**MVP scope:** Not in Prague v1. Not in fall 2026 v2. Target v3+ (2027).

### 5.10 Research Data Export

24. **Research dataset builder** — `axi classroom export --format parquet --anonymize` produces:
    - Anonymized interaction logs (student_id → pseudonym)
    - Per-student metrics summary
    - Quiz score timeseries
    - **Interview responses** (structured, from begin/end Q&A workflows)
    - Survey responses
    - Session classifications
    - RAG retrieval quality metrics
    All in a format suitable for pandas/R analysis and paper appendices.

25. **IRB/GDPR compliance** — Consent tracking per student. Data retention policy. Anonymization at export time. Students can request their data.

### 5.11 Federation Integration & Trust (added 2026-04-15)

> Status: ⬜ TODO. Federation foundation (ADR-022/023/024/025) is
> in place. This section maps classroom concepts to federation
> primitives so the classroom MVP can build on the foundation
> rather than alongside it.

**5.11.1 Classroom as an ephemeral federation.** Per ADR-023
lifecycle taxonomy, a classroom is the canonical example of an
**ephemeral federation**: created at enrollment, TTL'd to the
course end-date, archived per §5.9.5 on dissolution. Concrete
mapping:

| Classroom concept | Federation primitive (ADR-022/023) |
|-------------------|-----------------------------------|
| Course (reusable) | Federation spec template |
| Classroom (cohort instance) | Federation with `lifecycle=ephemeral` + declared `expires_at` |
| Instructor node | Federation root (single-key for MVP; quorum optional per ADR-024 Phase 1) |
| Student laptop / workspace | Federation member with `relationship=cluster` (intra-cohort) |
| Enrollment | `Membership` record issued by instructor, probation window = 0 or short for known-cohort students |
| Course end-date | Membership `expires_at` → auto-expire per ADR-023 §2 |
| Knowledge promotion (§5.9.5) | Membership-gated content promotion per content-tier policy |

**5.11.2 Student identity binding.**
- **MVP:** web-only (line 715). Session-token scoped; **no axiom
  node identity on the student laptop**. Avoids TOFU + install
  overhead for the 4-week course.
- **Phase 2+:** for iterative `/research` CLI access, students
  install axi with `axi install --profile student --federation
  <classroom-id>`. First-run ceremony: generate student keypair,
  register via instructor-signed invite token (ADR-016 §9), bind
  identity to classroom federation via multi-source attestation
  (ADR-023 §4 — invite + federation root + one peer student).

**5.11.3 Classroom trust policy profile.** Per
`project_trust_policy_profiles` memory — the classroom ships
with a default **`classroom-default`** trust profile:

| Category | Student action | Instructor action |
|----------|---------------|-------------------|
| RAG query on course corpus | Auto-approve silent | Auto-approve silent |
| Submit assessment response | Auto-approve audit | Auto-approve audit |
| Promote finding to cohort-shared | Prompt instructor | Auto-approve audit |
| Promote to institutional corpus | Prompt instructor (always) | Prompt instructor |
| Invite new member | Refuse (students can't invite) | Auto-approve audit |
| Cross-federation query (other cohorts) | Refuse | Prompt |
| Classification-tier elevation | Refuse | Refuse (requires separate authority) |

Instructors can tune sliders per-cohort; students cannot change
their own profile. `classroom-default` is signed by the Axiom
team as a shipped-default and by the instructor's institution
when adopted.

**5.11.4 Classification boundary in classroom.**

**Most classrooms operate at unclassified tier** — no special
handling required beyond the three-tier content model
(`spec-rag-architecture.md` public/restricted/export_controlled).

**Regulated-domain classrooms** may include
export-controlled content (e.g. 10 CFR Part 810 in some
engineering disciplines). Per `spec-classification-boundary.md §S6` and
§S7:

- Federation declares `export_regime` at creation; instructor
  sets nationality authorization list.
- Student identity attestations include nationality (signed by
  institutional registrar or equivalent).
- Deterministic per-access gate refuses export-controlled
  content to non-authorized nationalities, regardless of
  enrollment.
- Spillage response (§S10): if EC content enters the web chat,
  purge procedure applies; instructor notified; IRB/compliance
  escalation.
- Foreign-national students in a course with EC content: course
  manifest must declare which modules are accessible; EC
  modules gated per access.

**Classified-tier classrooms are Stage 3+** per
`spec-classification-boundary.md §7` staging — not in the
near-term roadmap.

**Acceptance criteria:**
- A student with nationality `X` attempting to access
  EAR-category `Y` content receives a deterministic refusal
  with audit log entry.
- A classroom configured `export_regime.foreign_nationals =
  prohibited` refuses enrollment of principals with non-US
  nationality attestations.
- Removing a module's EC stamp requires the instructor's
  signing key + the institution's export-control officer's
  signature.

**5.11.5 Quarantine & recovery for compromised student
accounts.** Per `project_quarantine_and_recovery_path` memory:

- Instructor invokes `axi classroom quarantine-student <id>
  --reason <human>`.
- Student's membership transitions to `QUARANTINED`; existing
  sessions dropped; prior work preserved for forensic review.
- Recovery ceremony requires (a) instructor signs unquarantine
  AND (b) student re-authenticates (new session token;
  identity rotation if CLI install and key was exposed).
- If compromise confirmed: escalate to `revoke-student`
  (permanent; prior work retained under revoked identity).

**5.11.6 Bonsai narration across classroom flows.** Per
`spec-security.md §2.4` and `project_extension_aware_rag_and_
bonsai` memory — Bonsai narrates:
- Enrollment ("you've joined Classroom X for Course Y, active
  until date Z, here's what access you have and what you can
  do")
- Approval gates (denied submission explains why + remediation)
- SCAN-detected signals ("the system noticed you've been stuck
  on this concept; here's a suggested next step")
- Instructor approvals (grading summary, cohort-pattern
  surfacing)
- Module access refusals when classification-gated ("this
  module requires X authorization; here's how to request it")

Deterministic gate stays authoritative; Bonsai only narrates.

**5.11.7 Always-on SCAN in the classroom.** Per
`project_eve_always_on_and_karpathy_loops` — SCAN operates in
cadence mode for the classroom:
- Per-student cadence: 30s during active session, 10min
  otherwise during enrolled period
- Signal sweep for engagement anomalies, objective-gap
  detection, help-request escalation, misconception pattern
  (§5.4.3 signal taxonomy)
- Promotion-threshold checks per Loop 6 (prd-auto-research
  §Loop 6) — gated by `classroom-default` trust profile

**5.11.8 Continuous improvement: cohort → course → shipped
default.** Per `project_platform_self_improvement_cascade`:
- Within a cohort: CURIO identifies content gaps,
  misconception clusters; instructor reviews.
- Across cohorts of the same course: aggregated patterns
  surface course-template improvements (opt-in).
- Across courses of the same institution: classroom-default
  trust profile refinements.
- Across institutions: opt-in contribution to Axiom's shipped
  default trust profiles + prompts + routing weights.

Every bubbling transition is OPT IN; audit-logged; reversible.

**5.11.9 Federation test harness for classroom.** Classroom
federation scenarios should live in
`tests/federation_lifecycle/test_classroom_*.py` — using the
existing harness. Scenarios to cover:
- ephemeral-federation-admission-with-probation
- classroom-end-date-membership-expiry
- student-quarantine-and-recovery
- foreign-national-EC-gate (consumer-specific; runs only when
  the domain is loaded)
- instructor-key-rotation-mid-semester
- cohort-cross-federation-query-refusal

### 5.12 Classroom PRD Acceptance Criteria Summary (added 2026-04-15)

Consolidated TODO list — cross-references existing sections.
Status legend: ✅ shipped · 🟡 partial · 📋 spec'd · ⬜ TODO.

**Phase 0 Foundation (pre-Apr-27):**
- ⬜ Langfuse trace integration (§5.1)
- ⬜ Web chat UI with streaming (§5.7)
- ⬜ Student token auth (§5.2)
- ⬜ Structured Q&amp;A engine (§5.5)

**Phase 1 Classroom MVP (Apr 28 → May 10):**
- ⬜ Course/Classroom entity model + ArtifactRegistry versioning (§5.3)
- ⬜ Enrollment + provisioning workflows WF-1..WF-6 (§5.8)
- ⬜ Federation ephemeral lifecycle mapping (§5.11.1) — **new**
- ⬜ Classroom trust profile (§5.11.3) — **new**
- ⬜ Classification boundary + export control (§5.11.4) — **new, BLOCKER for Prague if int'l students**
- ⬜ Quarantine + recovery path (§5.11.5) — **new**

**Phase 2 Intelligence + Federation (May 11 → May 25):**
- ⬜ Batch classifier + interaction metrics (§5.4)
- ⬜ SCAN always-on + signal taxonomy (§5.11.7) — **new**
- ⬜ Objective tracking alerts (§5.6)
- ⬜ Bonsai narration hooks (§5.11.6) — **new**
- ⬜ Federation test harness (§5.11.9) — **new**
- ⬜ AXI workflow orchestration

**Phase 3 Hardening (May 25 → class start):**
- ⬜ Bug fixes, corpus curation, instructor training
- ⬜ Full quarantine drill with simulated compromise
- ⬜ Bonsai classification-gate narration validated

**Phase 4 Post-MVP:**
- ⬜ Knowledge promotion ladder (§5.9)
- ⬜ Classroom archive + harvest (§5.9.5)
- ⬜ Alumni model (§5.9.6)
- ⬜ Cascade: cohort → course → shipped default (§5.11.8)
- ⬜ WF-7..WF-11 (withdrawal, instructor change, course review,
  presentations)

## 6) Non-Functional / Constraints

- **Latency:** Chat response < 5s p95 (including RAG retrieval). Students will abandon if slower than ChatGPT.
- **Availability:** Must work reliably for 4 weeks of daily use by 12 students. No tolerance for downtime during class sessions.
- **Security:** Student authentication via unique tokens (not passwords). TLS on the web endpoint. No student can see another student's sessions.
- **Privacy:** All data stays on infrastructure we control (no third-party analytics). LLM calls route through our gateway (we choose the provider).
- **Platforms:** Web chat must work on Chrome, Firefox, Safari (desktop + mobile). No native app required.
- **Scale:** 12 concurrent users max. This is not a scale problem.
- **Hosting:** The web endpoint must be reachable from the pilot site. Primary option: a self-hosted node (an on-campus tower) with a Cloudflare Tunnel for public HTTPS access. Fallback: cloud VM. Decision needed on tunnel approach.
- **Cost:** LLM API costs for 12 students x 4 weeks must be budgeted. Estimate: ~$200-500 depending on model choice and usage intensity.

## 7) Timeline

- **Phase 0 — Foundation (now → April 27):** Trace provider (Langfuse + LangSmith), web chat UI with streaming, student token auth, structured Q&A engine. Testable by instructors.
- **Phase 1 — Classroom (April 28 → May 10):** Classroom extension, cohort enrollment, course manifest, begin/end interview manifests, instructor dashboard.
- **Phase 2 — Intelligence (May 11 → May 25):** Batch classifier, objective tracking, SCAN classroom extractor, AXI agent, federation leaf-node topology. Load testing with simulated students.
- **Phase 3 — Hardening (May 25 → class start):** Buffer. Bug fixes, Langfuse deployment on the shared HPC cluster, corpus curation, instructor training. No new features.
- **Phase 4 — Live (during class):** Monitoring, daily data verification. Stability only.
- **Phase 5 — Research (after class):** Data export, analysis, paper writing.

*Timeline is aggressive by design. Phases may compress further depending on velocity.*

## 8) Risks & Open Questions

| Risk | Mitigation |
|------|------------|
| Web chat UX is worse than ChatGPT | Invest in streaming, markdown rendering, mobile responsiveness. User-test with a domain researcher before class. |
| LLM costs exceed budget | Set per-student daily token limits. Use smaller models for classification. Monitor daily. |
| Students don't use it | Make it required for certain assignments. Track engagement; intervene early. |
| RAG corpus quality is poor for course topics | Instructors curate and test the corpus before class. CURIO quality gates. |
| n=12 is too small for statistical significance | Use within-subject design (pre/post). Report effect sizes, not just p-values. Acknowledge limitation in paper. |
| Self-hosted node/tunnel reliability | Test tunnel from a representative network beforehand. Cloud VM fallback ready to promote. Coordinate with the deploying org's IT/security contact on org network policies. |
| IRB/ethics approval timeline | Check institutional requirements for human-subjects research. Start paperwork early. |

| Open Question | Decide By |
|---------------|-----------|
| Self-hosted node + tunnel vs. cloud VM? Tunnel type (Cloudflare / Tailscale / reverse SSH)? | May 1 |
| Which LLM provider/model for student-facing chat? | May 1 |
| IRB/ethics required? Which institution? | Course-dependent |
| Do students install anything or purely web? | Course-dependent — **Recommendation: web primary, CLI optional for advanced students** |
| Mobile support required or nice-to-have? | Course-dependent — **Recommendation: responsive web, no native app** |

## 9) Acceptance & Rollout

- **Sign-off:** Instructor lead (instructor experience), co-instructor (student experience), platform lead (technical + research design)
- **Rollout:**
  1. Internal dogfooding: the team and two domain researchers use web chat for 1 week
  2. Beta: 2-3 volunteer students (if available) test enrollment flow
  3. GA: Class start at the pilot site
- **Rollback:** If web chat fails catastrophically, students fall back to direct Claude/ChatGPT. We lose instrumentation but not the class.

## 10) Adoption Strategy

**Premise:** Some instructors and TAs will arrive preferring Claude (or
ChatGPT, or whatever general-purpose AI tool they already trust) and will
reasonably ask *"why do we need the consumer layer/Axiom for the classroom at all?"*
Any feature that merely duplicates what a general-purpose AI tool already
does adds friction without adding value and will be rejected. Axiom must
earn its place next to — not instead of — the tools these users already
love.

This section is a **first-class product commitment**. Every Prague-deployed
feature must map to at least one of the four prongs below; features that
map to none are adoption-at-risk and should be deprioritized.

### 10.1 Prong 1 — Go deep on genuinely differentiating value

These capabilities cannot be delivered by a stateless chat-only tool and
therefore cannot be matched by Claude/ChatGPT directly:

- **Federated memory across semesters.** Claude forgets every session.
  Axiom remembers cohort-level patterns (what worked, what failed, which
  misconceptions surface), preserves them across instructor cohorts via
  federation, and makes them available as institutional knowledge.
- **Per-student learning harvest (`.axiompack`).** Students leave Claude
  with nothing. Axiom gives every student a portable, signed, federation-
  compatible bundle of their learning trajectory — survives graduation,
  importable into their personal node forever.
- **Cohort-level pattern detection.** "7 of 12 students hit the same wall
  on chapter 4." A single-student chat tool cannot surface this. SCAN
  signal routing + classroom metrics aggregation does.
- **Provenance-audit grading.** Every score traces to the rubric clause,
  the student answer, the LLM rationale, the retrieved chunk, and the
  instructor-override note. Claude-graded work leaves no audit trail.
- **Cross-node export-control gate.** Mixed-nationality cohorts (Prague's
  case) need per-chunk EC filtering at retrieval time. Claude cannot
  enforce this.

### 10.2 Prong 2 — Integrate with existing workflows with near-zero friction

Skeptics don't switch tools. They accept additions that slot into tools
they already use. Axiom must meet them where they are:

- **Claude Code MCP server** exposing classroom read/write tools
  (`mcp__axiom_classroom__*`) so an instructor can ask Claude Code
  "show my cohort's stuck students" and get real Axiom data without
  typing a single `axi` command.
- **VS Code extension** for the grading queue, inline student-trace
  overlays, and jump-to-context from comments.
- **tmux status-line widget** showing live cohort pulse — active
  students, stuck-signal count, help-queue depth. Passive awareness;
  no dashboard tab to remember.
- **Shell completion** for `axi classroom *` in zsh/bash/fish so the
  CLI feels native.

### 10.3 Prong 3 — Tangential tease features (low-commitment, high-delight)

Small, genuinely useful features that show up in the daily workflow and
build trust without demanding loyalty:

- **`axi note`** — capture a freeform observation; auto-indexed to
  personal RAG; correlations surface later.
- **`axi classroom brief [--daily]`** — compiles the 3 things the
  instructor actually needs to know today. No dashboard; it arrives.
- **Side-by-side student answer comparison** — "how did 5 students
  answer question Q3?" Claude literally cannot do this.
- **One-click grade-explain trace** — the score plus every artifact
  that produced it. Every TA wants this.

### 10.4 Prong 4 — Viral adoption through students first

The fastest path to a skeptical instructor's trust is a student showing
up with something useful from the tool. Build features students care
about; the institutional adoption follows:

- **Per-student knowledge graph** surviving the course — visceral,
  portable, impressive at graduation.
- **`/help` chat command** routes a structured help ticket to the
  TA with recent turn context + misconception flag. The TA's first
  encounter is receiving a useful ticket they didn't have to
  ask for.
- **Session continuity across devices** via token-URL, no account
  friction.
- **Alumni transcript export + identity continuity** — students
  who graduate with their `.axiompack` become lifelong Axiom users;
  future cohorts benefit from the network.

### 10.5 Operational implications

- **PR gating:** reviewers ask "which prong does this map to?" during
  Prague-track reviews. Unclear answer → move to non-classroom backlog.
- **Feature prioritization:** prong 1 and prong 4 dominate the
  critical path (differentiation + virality). Prong 2 and 3 are
  enablers.
- **Evaluation metric:** would a Claude-only instructor/student notice
  the difference? If no, the feature is adoption-at-risk.

### 10.6 Memory network positioning

Axiom does not compete with personal-memory tools (Mem0, Letta,
ChatGPT memory, Claude memory, Supermemory, etc.). Anthropic made
personal memory free in March 2026; several open-source frameworks
ship superior personal memory at the Apache-2.0 level. That layer
is a commodity.

Axiom's position is **"the memory network for regulated knowledge
work."** Personal memory is a commodity substrate we embed (Mem0
for vector, Graphiti for temporal KG, Letta for stateful runtime);
the defensible layer is what we build on top:

- **Federated memory across organizations** (academic only; no
  commercial implementation exists — see Rezazadeh et al. 2025,
  arXiv 2505.18279 *Collaborative Memory*).
- **Cryptographic provenance per fragment** (ADR-028) — Content
  Authenticity Initiative for agent memory.
- **Trust graph with UX for sharing policy** (ADR-028 +
  trust-policy-profiles) — how much memory goes to whom, tuned
  per-context.
- **Cross-organization aggregation with export-control awareness**
  (ADR-027 + classroom EC gate) — impossible in Glean (single-org)
  or ChatGPT (walled garden).
- **Ownership that transcends node/agent boundaries** (ADR-026) —
  masters retain authority even as content flows through federation.

For classroom specifically: the classroom is the **beachhead proof
point** for the memory network. Students who graduate with their
`.axiompack` (harvest, ADR-026 transfer ceremony) become lifelong
Axiom users — the network effect bootstraps from our academic
wedge into professional and research markets.

**Evaluation metric (second pass):** does this feature strengthen
the memory network for regulated knowledge work? If yes, critical.
If no, re-evaluate against the four adoption prongs.

---

## 11) Future Considerations

Single-entry section. Graveyard of grand ideas kept to one so the PRD
tells us what's being built, not what might be. New entries require
retiring existing ones.

### Self-evolving syllabus (post-v2)

The course syllabus learns from cohort outcomes across semesters.
Aggregate signals — "students consistently struggle at day 8," "this
assessment correlates poorly with learning-objective coverage,"
"chunk-X retrieval precision is degrading" — drive instructor-
reviewed proposals to rearrange, add, or prune course content.
Federated: proposed changes visible across institutions using the
course, with author attribution + opt-in merge. Instructor retains
approval authority; the system proposes, the instructor decides.

**Why this and not the others:** it's the only candidate whose value
*strictly compounds* with continued use across semesters, and whose
architectural requirements (multi-semester federated cohort state
with provenance) match nothing else on the market. Every other
candidate Claude or a competitor could plausibly match within 18
months.

**Prerequisites:** requires matured classroom metrics (v1), course
versioning + semver (v1 — already built), federation A2A transport
(v2), at least 3 cohort iterations of instrument data (v2).

**Target:** v3 or later. Not a Prague MVP.

---

## 12) Contacts & Links

- Product lead: Benjamin Booth (no-reply@axiom-os.ai)

- PRD: this document
- Tech spec: `docs/specs/spec-classroom.md`
- First deployment: a domain summer course at the pilot site (two domain researchers, 12 students)

---
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
