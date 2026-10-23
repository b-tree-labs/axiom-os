# Product Requirements: Alumni & Longitudinal Identity

**Product / Feature:** Axiom Alumni System

**Owner:** Benjamin Booth  •  **Status:** Draft  •  **Last updated:** 2026-04-13

---

## Table of Contents
- [1) Elevator Pitch](#1-elevator-pitch)
- [2) Problem / Opportunity](#2-problem--opportunity)
- [3) Goals & Success Metrics](#3-goals--success-metrics)
- [4) Key Users / Personas](#4-key-users--personas)
- [5) Scope — Key Capabilities](#5-scope--key-capabilities)
- [6) Non-Functional / Constraints](#6-non-functional--constraints)
- [7) Timeline & Phasing](#7-timeline--phasing)
- [8) Risks & Open Questions](#8-risks--open-questions)
- [9) Contacts & Links](#9-contacts--links)

---

## 1) Elevator Pitch

A classroom is an event. A Course template is persistent. **Alumni are the living bridge between them** — the people who completed a Course, with permanent attribution of their contributions, opt-in agent presence for future students, and consented longitudinal research participation. This turns one-time instruction into a learning community that spans cohorts and decades.

## 2) Problem / Opportunity

- Today's classroom platforms treat students as transient — when the course ends, the student disappears from the system. Their contributions, if any, lose attribution.
- Longitudinal educational research is nearly impossible at scale because researchers can't find their subjects 2-5 years later. Most studies die for lack of follow-up.
- Course templates accumulate corpus content across semesters, but never accumulate *people*. A 2035 student can't ask a 2026 alumnus "did this end up mattering in your career?"
- Attribution of student research contributions is fragile. When the classroom archives, the attribution chain should persist — students should cite their own earlier contributions; instructors should show pipeline of contributor → course template improvements.
- Alumni networks exist everywhere (LinkedIn, institutional alumni offices) but none are agent-mediated, scope-limited, or integrated with learning platforms.

## 3) Goals & Success Metrics

- **Primary goal:** Every student who completes a Course gains an opt-in persistent alumnus identity that preserves their contributions, enables longitudinal research participation, and optionally allows future students to learn from them via federation.
- Success metrics:
  - ≥80% of completing students opt in to alumnus record creation.
  - 100% of promoted findings retain alumnus attribution permanently.
  - Longitudinal surveys achieve ≥40% response rate at 1 year post-course (vs. <10% industry standard for traditional follow-up).
  - At least one Course template accumulates 3+ cohorts of alumni contributing to its corpus across semesters.
  - Zero incidents of alumni identity data being used without consent.

## 4) Key Users / Personas

- **Alumnus:** A person who completed a Course and opted in to persistent identity. Controls what's visible, what's queryable, what they participate in. Can revoke or modify consent at any time.
- **Current student:** May benefit from alumni contributions (cited in RAG, or directly queryable via agent federation if alumni opted in). Gains a sense that they're joining a community, not just taking a course.
- **Instructor:** Can broadcast to alumni networks (scoped to their Course templates) for specific purposes — "current cohort has a question about X, did any of you end up working on that?" Also reviews longitudinal research outcomes.
- **Researcher:** Conducts longitudinal studies on educational outcomes. Can follow cohorts 1/3/5 years post-course via consent-gated questionnaires.
- **Federation peer:** Other institutions can see alumni contributions (within access-tier rules) but cannot contact alumni directly without the alumnus's explicit permission.

## 5) Scope — Key Capabilities

### 5.1 Alumnus Record

Created when a student completes a Classroom (`completed` state) and transitions into alumnus status — opt-in during onboarding consent and confirmable at classroom archive.

```yaml
alumnus_id: alum-prague-ne-2026-student07
course_template: ne-fundamentals-v2
classroom_instance: prague-ne-2026
completion_date: 2026-07-26
final_status: completed  # completed | withdrawn | failed | incomplete
consent:
  perpetual_identity: true
  contribution_attribution: true
  federation_participation: true
  federation_visibility: public | trusted_peers_only | private
  longitudinal_surveys: true
  alumni_network_messaging: true
contributions:
  findings_promoted:
    - f-fecral-creep-resolution (course_rag)
    - f-smr-licensing-pathway (course_template_rag)
  presentations_archived:
    - pres-atf-neutronics
  harvest_bundle_id: harvest-prague-ne-2026-student07
  harvest_bundle_location: <alum's-node-uri>
```

Alumnus records are ArtifactRegistry items — versioned, portable, federatable. Stored at the Course template level (`course_template/ne-fundamentals-v2/alumni/`), not the classroom instance.

### 5.2 Attribution Permanence

Every promoted contribution carries its attribution chain forever. When a finding cites an alumnus, that citation travels with the finding wherever it goes — course RAG, org RAG, community RAG, other institutions' federated corpora.

Alumni can update their preferred display name or contact email over time, but cannot delete attribution (would break citation chains). They *can* withdraw from further use:

```yaml
attribution:
  original_contributor: alum-prague-ne-2026-student07
  contributor_status: active | withdrawn | deceased
  withdrawal_date: 2028-03-15  # if withdrawn
  withdrawal_note: "Contributor requested attribution retention but no further use"
```

When a contributor withdraws, findings remain cited ("contributed by [alum-id], withdrew further participation 2028-03-15") but they are not contacted for follow-up, their alumnus CURIO is deactivated, and their name can be replaced with the alum-id.

### 5.3 Alumnus Agent Presence

Opt-in: alumni can maintain a CURIO instance that represents their course-derived knowledge. `@alum-prague-ne-2026-student07-curio` responds based on what the alumnus studied + what they've chosen to make shareable.

**Scope-limited by default:**
- Responds only to queries about topics the alum opted in to discuss
- Has a rate limit (alumni are not infinite-free support)
- Can be silenced by the alum at any time
- Cannot reveal private information, restricted-tier content, or anything the alum hasn't explicitly made available

**Use cases:**
- A current student asks `@alum-prague-ne-2026-student07-curio what was the most challenging part of the four-factor formula for you?` — the alumnus CURIO answers based on their historical session data (if opted in) and reflection
- A researcher asks about career trajectory with consent
- Another alumnus reaches out through their Course template network

### 5.4 Alumni Network Federation

All alumni of a Course template form a lightweight federation scoped to that Course. The Course template acts as a directory — alumni records are indexed but visibility is controlled per-alumnus.

Instructors can broadcast to their alumni networks (agent-mediated, not bulk email):

```
Instructor (to AXI): Ask alumni of ne-fundamentals-v1 and v2 
                       if any of them ended up working on 
                       accident-tolerant fuels in industry. 
                       Current cohort has a question.

AXI: Broadcasting query to 47 opted-in alumni across 3 cohorts.
        Responses will be collected asynchronously over 7 days.
        Alumni who respond can choose to be directly introduced to 
        the asking student or remain pseudonymous.
```

This is **not** social media. It's purpose-bound, agent-mediated communication scoped to a Course template's alumni network, with consent at every step.

### 5.5 Longitudinal Research Participation

Alumni who opt in to longitudinal surveys receive structured Q&A questionnaires at configurable intervals (typical: 1 year, 3 years, 5 years post-completion).

Questions like:
- Did what you learned in this course end up being useful in your career? How?
- Which concepts have you applied most?
- Which concepts turned out to be more/less important than the course emphasized?
- What would you tell a current student starting this course?

Responses are structured (typed questions), stored as research data, and — with consent — aggregated for publication. Attribution is controlled by the alumnus (anonymized aggregate is default; individual quotes require additional consent).

This is the capability that makes a research program possible. Most longitudinal studies fail because researchers can't find their subjects. Alumni-as-federation-identity makes them findable, opt-in reachable, and agent-addressable indefinitely.

### 5.6 Consent Management

Consent is the foundation of this entire system. Core principles:

- **Granular:** Separate consent flags for identity persistence, attribution, agent presence, longitudinal surveys, alumni network messaging.
- **Modifiable:** Alumni can change consent at any time through their Axiom node.
- **Revocable:** Any consent can be withdrawn. Revocation has specific, defined consequences (attribution retained but marked withdrawn; CURIO deactivated; no further surveys).
- **Portable:** Consent records live with the alumnus's Axiom node, not the institution's.
- **Auditable:** Every consent state change is logged with timestamp and reason.

Consent is captured in two phases:
1. **Onboarding (WF-2):** Prospective consent — "if you complete the course, do you want to opt in to these things?" Can be changed before completion.
2. **Archive (5.9.5):** Final consent — confirms or modifies onboarding choices at the moment of alumnus record creation.

Students can always change their minds later.

### 5.7 Ownership Model

**Alumni data is owned by the alum, not the institution.** This is a hard constraint.

- Harvest bundle lives on the alum's Axiom node (their node, their storage).
- Institution has a *pointer* (the alumnus record) and can cite attribution, but cannot delete the alum's bundle, force updates, or access private content.
- If the institution shuts down or the Course template is deprecated, alumni records remain valid. Alumni CURIO instances continue operating from their owners' nodes.
- Federation to the institution's RAG is always with the alumnus's permission and can be revoked.

## 6) Non-Functional / Constraints

- **Privacy:** All consent is opt-in, never opt-out by default. No dark patterns. IRB/GDPR compliance required for research use.
- **Longevity:** Alumnus records must survive institutional changes, platform migrations, and decade+ timeframes. Design for permanence.
- **Attribution integrity:** Citations must survive alumnus consent changes — withdrawal removes future use, not past use. Academic integrity requires this.
- **Agent rate limits:** Alumnus CURIO instances have strict rate limits (alumni are not on-call support for future students).
- **Cost:** Running opted-in alumnus agents costs LLM tokens. Model: alumni pay nothing; receiving institution pays for queries to their Course template's alumni network.
- **Scalability:** Course templates can accumulate thousands of alumni over decades. Directory and federation protocols must scale to that.

## 7) Timeline & Phasing

This is a **post-MVP, v3+** feature. Not in Prague MVP. Not in fall 2026 v2. Target: 2027+.

**Prerequisites:**
- Classroom archive & harvest (v1 scaffolding, v2 completion)
- ArtifactRegistry at Course template level
- Federation protocol with agent identity
- Consent management infrastructure
- Natural-language policy broadcasting (for alumni RACI delegation)

**Phased delivery:**

| Phase | Capabilities |
|-------|--------------|
| **A: Identity** | Alumnus record creation on classroom completion. Permanent attribution for promoted contributions. Static alumnus records, no agent presence. |
| **B: Longitudinal surveys** | Consent flow, scheduled questionnaire delivery, response aggregation, basic research export. |
| **C: Agent presence** | Opt-in alumnus CURIO instances. Federation addressing. Scope limits + rate limits. |
| **D: Alumni networks** | Cross-cohort federation under Course template. Instructor broadcasts. Alumnus-to-alumnus messaging (scoped). |

## 8) Risks & Open Questions

| Risk | Mitigation |
|------|-----------|
| Alumni feel "tracked" or surveilled | Strict opt-in defaults. Transparent consent UI. Easy withdrawal. Regular reminders of current consent state. |
| Attribution becomes a harassment vector | Alumni can use pseudonymous IDs by default. Direct contact always requires both parties' consent. |
| Agent-mediated alumni queries reveal more than intended | Default scope is tight. Alumni review and approve what topics their CURIO can discuss. Sample queries shown before opting in. |
| Institutional ownership dispute | Clear constitutional principle: alumnus data is owned by alum, not institution. Codified in data ownership agreements. |
| Alumni abandon their nodes (node death) | Course template archives a read-only snapshot of contributions (not the full bundle) so citations survive even if the alum's node disappears. |

| Open Question | Decide By |
|---------------|-----------|
| What happens to alumnus records when a Course template is deprecated? | Before Phase A ships |
| How do we handle multi-institutional alumni (student took courses at UT and INL)? | Before Phase D |
| Should there be an "emeritus" status for highly-contributing alumni? | Phase D |
| How do we verify alumnus identity for sensitive actions (withdrawal, attribution changes) over decade timeframes? | Before Phase A |
| What's the retention policy for alumnus data after contributor death? (legal + ethical complexity) | Before Phase B |

## 9) Contacts & Links

- Product lead: Benjamin Booth (no-reply@axiom-os.ai)
- Parent PRD: `docs/prds/prd-classroom.md` (Section 5.9.5 — Archive & Harvest; Section 5.9.6 — Alumni reference)
- Related: `docs/working/curio-research-loop.md` (federation, attribution), `docs/working/natural-language-policy-broadcasting.md` (RACI at cohort/network scope)

---
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
