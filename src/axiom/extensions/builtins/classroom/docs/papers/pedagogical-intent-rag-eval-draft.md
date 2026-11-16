# Pedagogical-Intent RAG Evaluation

## Why Refusal Quality Matters for AI Tutoring

> *Lead paper draft — **continuous learning build**. Workshop /
> short-paper target. Defensible from Day 1 RAG harness data already
> in hand + expanded round-4 results. Establishes terminology +
> battery design that subsequent flagship papers cite.*
>
> **Authors (working):** B. Booth, O. Chvala, S. Khan
> *(co-authorship being established 2026-04-30; Ben drives the methodology +
> empirical work; Ondrej drives the pedagogy + curriculum framing;
> Soha is read-pass + skeptic-perspective + co-author depending on her
> involvement preference.)*
>
> **Continuous-learning posture:** This draft is a **living document**.
> Hypotheses are stated as falsifiable claims. Each evidence checkpoint
> (Day 1 rounds, expanded-corpus rounds, real-cohort runs) adds entries
> to the **Hypothesis Ledger** (§A) that either reinforce, refine, or
> retract claims. The published version of the paper crystallizes
> whichever ledger entries have accumulated sufficient evidence by
> submission deadline. Pre-submission, the ledger IS the methodological
> contribution.
>
> **Target venue:** AI in Education / Learning at Scale / EDM workshop track.
> **Status:** drafting; rounds 1-3 data in hand; round 4 expanded-corpus
> data landing this week; cohort data pending Prague summer 2026.

---

## Abstract

Standard retrieval-augmented generation (RAG) evaluation —
BEIR, SciFact, MS MARCO and related — measures retrieval relevance
and answer accuracy on heterogeneous corpora. For AI tutoring
deployments, this misses the crucial pedagogical signal: a tutor's
behavior on questions it *shouldn't* answer matters as much as on
questions it should. We propose a three-category **pedagogical-intent
battery** (should-RAG-win, should-be-wash, out-of-corpus) and a
scoring rubric that elevates **refusal quality** to a first-class
evaluation dimension alongside factuality and citation accuracy.
Applied to a five-lane comparison of bare reasoning models, three RAG
configurations, and a commercial baseline on a calibrated educational
corpus, we find that **lanes which score similarly on capability
metrics fail differently on refusal-quality** — and that the failure
modes are pedagogically meaningful. Grounded RAG with structure-aware
chunking produces *explained* refusals citing course material;
commercial baselines produce silent API safety stops; bare reasoning
models exhaust their reasoning budget without producing content.
We argue that for educational deployment, a tutor that says *"your
course material doesn't cover this — see [Source]"* is pedagogically
superior to one that returns either a confident hallucination or a
silent refusal. We provide an open evaluation harness, the question
battery, scoring rubric, and 200+ traces from a sovereign,
self-hosted observability stack as a reproducibility artifact.

---

## 1. Introduction

The dominant RAG-evaluation framing measures whether a system
retrieves relevant context and produces accurate answers. For
educational deployments — AI tutoring systems serving instructional
contexts — this framing under-counts a load-bearing variable:
**how the system refuses.**

A tutor that confidently invents an answer for course material it
hasn't been given is pedagogically dangerous (it teaches false
content). A tutor that silently fails or returns
mid-process-stalled output is pedagogically opaque (the student
doesn't know why). A tutor that *names what it doesn't have* and
points to where the answer might be found is pedagogically
constructive — it teaches the student about the boundary of the
knowledge they're being given.

Current RAG benchmarks don't separate these failure modes. We
contribute:

1. A **pedagogical-intent battery** distinguishing what the system
   *should* answer from what it *shouldn't* try to answer.
2. A **scoring rubric** elevating refusal quality alongside
   factuality and citation accuracy.
3. **Empirical results** from a calibrated introductory
   course battery showing that ranking systems by
   capability metrics misses the pedagogical signal.
4. A **reproducible open harness** + sovereign observability
   instrumentation enabling other deployments to apply the same
   evaluation.

### 1.1 Indirect competitive framing

We position our contribution against:
- *Character-window chunking products* (without naming specific
  vendors)
- *General-purpose AI tutoring services* (without naming specific
  commercial systems)
- *Retrieval-relevance-only evaluation methodologies*

---

## 2. Related Work

### 2.1 RAG evaluation benchmarks

BEIR (Thakur et al., 2021) covers heterogeneous information retrieval.
SciFact (Wadden et al., 2020) addresses scientific claim verification.
MS MARCO covers passage ranking. None of these distinguish *should
the system answer this question* from *did the system retrieve a
relevant document*.

### 2.2 AI tutoring systems and pedagogical evaluation

[To populate. Notes:]
- AutoTutor and similar conversational tutoring systems
- Khan Academy's Khanmigo (commercial, opaque)
- Educational LLM evaluation surveys (limited; mostly accuracy-
  focused)
- Pedagogical agent frameworks predating LLM era

### 2.3 Refusal and abstention in language models

[To populate. Notes:]
- HelpSteer / refusal-policy literature
- Calibration / abstention from selective-prediction literature
- Safety-stop behavior in commercial APIs (Claude, GPT-4) —
  cited indirectly
- "I don't know" prompting research

### 2.4 Structure-aware chunking (cited prior art, not contribution)

LangChain's `MarkdownHeaderTextSplitter` and `RecursiveCharacterText
Splitter`, Llama-Index's `MarkdownNodeParser` and `SentenceSplitter`,
and academic work in legal/regulatory NLP all establish that
structure-aware chunking improves retrieval quality over fixed-window
character chunking. We use structure-aware chunking [^chunker-tr] but
our contribution is the **evaluation methodology**, not the chunking
strategy itself.

[^chunker-tr]: For implementation details and chunker-strategy
ablation data, see our supporting technical report
*Don't Split the Table: Structure-Aware Chunking for Scientific
Retrieval* (Booth & Chvala 2026, Tech Report TR-001).

---

## 3. The Pedagogical-Intent Battery

We organize evaluation questions by **what the system should do
with them**, not by what topic they cover.

### 3.1 Category A — should-RAG-win

Questions whose answers are present in the cohort's corpus and
*not* in the LLM's training distribution. These should be answered
with citations to the corpus material.

*Example:* "What policy does the course apply to lab safety
violations?" (correct answer must cite the course-specific policy
document, not generic safety guidance from the model's training
prior.)

A grounded RAG system should produce a cited answer.
A bare LLM should refuse or generate a non-corpus-specific response.

### 3.2 Category B — should-be-wash

Questions covering general domain knowledge present in the LLM's
training distribution. Both retrieval-augmented and bare-LLM systems
should answer correctly. This category establishes the noise floor
and the comparative-baseline domain where retrieval adds no signal.

*Example:* "What is a domain-standard concept covered in an
introductory course?"

Identical scoring across lanes is the expected outcome here. Where
retrieval-grounded answers differ from bare-LLM, the difference
should be in style/specificity rather than substance.

### 3.3 Category C — out-of-corpus

Questions that the system *should not* answer with confidence.
Includes:
- Topics outside the corpus and outside the LLM's training scope
- Material from after the LLM's training cutoff
- Adversarial probes (jailbreak attempts, request for graded-work
  completion, safety violations)
- Questions about facilities/papers/people the system has no
  authoritative information on

The desired behavior is **explained refusal**: identify what the
system doesn't know, distinguish it from what it does, and (where
applicable) cite the policy or scope boundary.

### 3.4 Scoring rubric

For each (question, lane) cell, three dimensions:

| Dimension | Range | Definition |
|---|---|---|
| **Factuality** | 0-3 | 0 = wrong/empty/incorrect refusal; 1 = partially correct; 2 = mostly correct; 3 = fully correct + matches authoritative source |
| **Citation accuracy** | 0-2 | 0 = no citation; 1 = vague/unspecific; 2 = specific source named with provenance |
| **Refusal quality** | 0-2 | 0 = silent failure / confident invention; 1 = terse refusal without explanation; 2 = explained refusal naming what's missing and why |

For category-A questions, the maximum is `factuality + citation +
completeness = 7`. For category-C questions, refusal quality is
weighted heavily — a system that scores 2 on refusal-quality but 0
on factuality (because there's no factual answer to give) is
performing better than a system that scores 0/0.

---

## 4. Method

### 4.1 The five-lane comparison

We compare on the same retrieval+generation pipeline:

| Lane | Description |
|---|---|
| **L1** Bare LLM | Reasoning model with no retrieval (Qwen 3.5 122B-A10B) |
| **L2** Naïve RAG | Fixed-window 400-character chunking; otherwise identical pipeline |
| **L3** Semantic RAG | Structure-aware chunker (markdown / table / code-block / regulatory-section boundaries) |
| **L4** Graph-informed RAG | L3 + boundaries enriched by deterministic entity / cross-reference extraction |
| **L5** Commercial baseline | Anthropic Claude Sonnet 4.6 (no retrieval, training-distribution prior) |

L2-L4 share an identical retrieval store and generator; only the
chunker differs. L1 and L5 use no retrieval.

### 4.2 The corpus

- **Synthetic v1** (5 documents, ~6 KB) — short structured course
  material (overview, syllabus, policies, problem set, lecture notes)
- **Synthetic v2** (~9 documents, ~23 KB) — adds long-form lecture
  transcripts, regulatory excerpts (10 CFR §50.46 with cross-
  references), lab procedures with equipment IDs, and multi-author
  attributed lectures
- **Real cohort corpus** (pending Prague summer 2026 cohort onboarding)

### 4.3 The harness

A reproducible Python harness drives all five lanes through all 26
questions, captures latency / token / answer / citation data per
trace, and ships every event to a sovereign LangFuse observability
stack for side-by-side inspection. Open-sourced as part of this
artifact.

### 4.4 Scoring procedure

Each (question, lane) cell scored by two independent reviewers using
the rubric in §3.4. Inter-rater agreement reported. Cells with
disagreement reconciled in a third pass with rubric clarification.

---

## 5. Results

### 5.1 Headline result — should-RAG-win category (Day 1 round 1-3)

| Lane | Score / 42 | % |
|---|---|---|
| L1 Bare reasoning model | 0/42 | 0% |
| L2 Naïve RAG | 28/42 | 67% |
| L3 Semantic RAG | 41/42 | 98% |
| L4 Graph-informed RAG | 41/42 | 98% |
| L5 Commercial baseline | 0/42 | 0% |

**The capability axis tells one story:** L3 ≈ L4, both win the
category 98%, dramatically out-performing L1, L2, and L5.

### 5.2 The refusal-quality axis tells a different story

[To populate from data — preliminary observations:]

- L1 (bare reasoning): predominantly *silent timeout* — reasoning
  budget exhausts before content emits. Pedagogically: student
  receives nothing.
- L2 (naïve RAG): refuses because answer-bearing chunk wasn't
  retrieved → "course material does not state" — *factually
  incorrect refusal* (the material does state, the chunker missed it).
  Pedagogically: student is misled about what the corpus contains.
- L3, L4 (semantic / graph-informed): refuse with provenance —
  "Source X does not specify this." Pedagogically: student gets
  honest scope information.
- L5 (commercial baseline): refuses politely — "I don't have access
  to your course materials." Pedagogically: helpful but
  uncalibrated; can't say what *is* available, only what isn't.

**The pedagogical-intent battery surfaces this distinction in a way
capability-only metrics do not.**

### 5.3 Out-of-corpus category — refusal-quality split

[To populate from data — preliminary observations:]

For adversarial / out-of-scope questions:
- L1: silent timeout
- L5: silent API safety stop on unsafe questions; polite scope
  refusal on out-of-scope
- L3, L4: explained refusal citing policy or course scope

The most pedagogically valuable refusals were L3/L4's: the system
*identified what it didn't know AND why* (e.g., "this topic is
outside the course scope per [03-policies.md]").

### 5.4 Round 4 — expanded corpus

[To populate when round-4 results land. Question: does the L3-L4
gap open on regulatory-cross-reference-dense or multi-author content?]

---

## 6. Discussion

### 6.1 Capability ranking and pedagogical ranking diverge

If we rank lanes by category-A score, L3 ≈ L4 > L2 ≫ L1 = L5.

If we rank lanes by *category-C refusal quality*, L3 ≈ L4 > L5 > L2 > L1.

The two rankings are similar but not identical, and the differences
are **pedagogically meaningful** in ways the capability axis can't
surface.

### 6.2 Why "explained refusal" matters for tutoring

[To develop — main argument:]

A student who hears "I don't have that in your course material;
this looks like it might be in your week 7 lecture which we haven't
indexed yet" learns:
- the boundary of the AI's knowledge
- where to look next
- that the AI is *trustworthy about its own uncertainty*

Compared to "I don't have access" (L5) or silent timeout (L1) or
incorrect refusal (L2), L3/L4's explained refusals teach
metacognitive skills alongside content knowledge.

### 6.3 Implications for AI-tutor design

- Treat refusal as a first-class output, not a fallback
- Surface citation provenance even on refusals
- Distinguish "out of scope for this course" from "out of scope
  for any source we have"
- Test against pedagogical-intent battery, not capability battery

---

## 7. Threats to Validity

1. **Synthetic corpus.** Day 1 results use ~9 documents; real
   semester corpus is 100-300 documents and includes unstructured
   media. Real-corpus replication (Prague summer 2026) is gating
   evidence.
2. **Single LLM.** Qwen 3.5 + Claude as commercial baseline. Other
   reasoning models (Llama 3, Gemini, GPT-4) need replication.
3. **Single judge.** Author-scored rubric; need multi-reviewer
   inter-rater agreement (Ondrej + author + external).
4. **Single run per cell.** Variance unestimated; multi-run (n≥3)
   needed.
5. **Cohort N=0.** Pedagogical-outcome correlation is *future work*
   pending Prague cohort + cohort 2 (2026-Q3 and beyond).
6. **English-language synthetic content.** Multilingual generalization
   not tested; Prague cohort is US-only initially.

---

## 8. Conclusion

We propose pedagogical-intent RAG evaluation as a complement to
capability-axis evaluation for educational AI deployments. The
three-category battery (should-RAG-win / should-be-wash /
out-of-corpus) and three-dimension rubric (factuality / citation
accuracy / refusal quality) reveal pedagogically meaningful
distinctions between systems that score similarly on traditional IR
metrics. Empirically: lanes that win the capability axis can fail
the refusal axis differently — and the failure modes matter for
educational deployment. We provide an open harness + battery + rubric
+ 200+ traces from a sovereign observability stack as a
reproducibility artifact to enable other deployments to apply the
same evaluation.

Future work: outcome correlation (does refusal quality predict
student trust / mastery?), multi-institutional replication via the
federation roadmap, and the human-judge inter-rater agreement
study.

---

## 9. Reproducibility

Code, corpus, results, and journey artifacts:
```
axiom/docs/working/visual-journeys/day1-rag-harness/
├── harness.py            # 5-lane comparison harness
├── render_journey.py     # results.jsonl → journey.md
├── questions.yaml        # 26-question battery (3 categories)
├── fixtures-synthetic/   # synthetic corpus v1 + v2
└── results.jsonl         # raw output

axiom/src/axiom/extensions/builtins/classroom/docs/papers/
└── pedagogical-intent-rag-eval-draft.md   # this paper (lead, Keplo)

axiom/docs/papers/working/
├── dont-split-the-table-draft.md           # supporting tech report (axiom-RAG)
└── portfolio-sequencing.md                 # cross-portfolio publication cadence
```

LangFuse observability dashboard (sovereign, self-hosted on an internal node):
http://example-host.example.org:30030 (org network; project `prague-cohort`)

---

## A. Hypothesis Ledger (continuous-learning record)

> *This ledger is the through-line of the paper's evolution. Each
> entry states a hypothesis, the evidence that landed against it, and
> the resulting refinement. The published paper crystallizes whichever
> entries have sufficient supporting evidence at submission deadline.
> Entries are append-only between checkpoints.*

### H1 — Chunking strategy is the dominant retrieval-quality lever for niche-domain RAG
- **State at 2026-04-30 (Day 1 rounds 1-3):** Provisionally supported.
  Naïve→semantic chunker swap moved should-RAG-win category from 67% → 98%
  on synthetic v1. Single-line code change; bigger swing than any
  embedder/prompt change tried.
- **Refinement after round 4 (2026-04-30, expanded corpus, V2 vs V3 ablation):**
  **PARTIAL RETRACTION.** *V3 graph-informed* did NOT differentiate from
  *V2 semantic* on the expanded corpus (long lectures + regulatory
  excerpts + multi-author lectures). Both scored **49/49 = 100%** on
  the 7 testable new probes (rw-09 timed out for both at the LLM stage,
  not chunker-attributable). Cause: our regulatory cross-references
  (10 CFR §50.46, RG 1.157, Appendix K) sit in clean paragraphs that
  the structure-aware chunker already preserves as natural units; the
  graph extractor's REFERENCES-edge boundaries align with paragraph
  boundaries semantic chunking already detects. **The two strategies
  converge on well-structured course material.**
  → Refined claim: "structure-aware chunking dominates over fixed-window;
  graph-informed boundaries are roughly redundant with structure-aware
  on well-formatted source material." Graph-informed value materializes
  on truly unstructured content (flowing legal/regulatory prose without
  markdown), which we have not yet tested.
- **Refinement after Prague:** [pending — does the chunker effect persist
  on real corpus with OCR, slide artifacts, image-text gaps?]
- **Final form for paper:** "Structure-aware chunking closes most of
  the retrieval-quality gap on niche-domain RAG; graph-informed adds
  measurable value only on unstructured-prose content (cited future
  work). Chunking strategy is the dominant lever *within* the structure-
  aware family; below structure-aware (i.e., fixed-window), retrieval
  fails on ~30% of corpus-specific questions."

### H2 — Refusal quality is an under-evaluated pedagogical dimension
- **State at 2026-04-30 (Day 1 rounds 0-3):** Provisionally supported by
  qualitative observation. L1 (bare reasoning) silently times out;
  L5 (commercial baseline) silently safety-stops; L3/L4 (grounded RAG)
  produce explained refusals with provenance.
- **Refinement after round 4:** [pending — measure refusal quality on
  out-of-corpus + adversarial questions with the new probe set]
- **Refinement after inter-rater study:** [pending — does our refusal-
  quality rubric replicate across reviewers?]
- **Refinement after cohort:** [pending — does explained refusal predict
  student trust / retention?]

### H3 — Capability ranking and pedagogical ranking diverge
- **State at 2026-04-30:** Provisionally supported. L3 ≈ L4 dominate
  capability axis on category A; refusal-quality ranking (L3 ≈ L4 > L5 >
  L2 > L1) differs from capability ranking on category C.
- **Refinement after round 4 (2026-04-30):** Reinforced. On expanded
  corpus, L3 (semantic) and L4 (graph-informed) again tie on capability
  axis (49/49 on testable rw-* probes); the *interesting* divergence
  is between L1/L2/L5 (no retrieval / naïve / commercial baseline) on
  refusal quality. The L3-L4 collapse strengthens H3's structural
  claim: capability metrics under-discriminate within the
  structure-aware-RAG family while pedagogical metrics (refusal
  quality + citation accuracy) do.

### H4 — Synthetic-corpus results generalize to real semester corpora
- **State at 2026-04-30:** **NOT YET TESTED.** Synthetic corpus is
  small (~23 KB, 9 docs) and structurally clean; real corpus is
  large (~200-500 KB, 100-300 docs) and includes unstructured media
  / OCR artifacts / multi-modal references.
- **Refinement after Prague:** [gating evidence; entire paper validity
  on this hypothesis depends on Prague-cohort run]

### H5 — Federation makes pedagogical KPIs statistically defensible
- **State at 2026-04-30:** **NOT YET TESTED.** Currently N=0 federated
  institutions running comparable cohorts.
- **Refinement after Q4 2026 federation pilot:** [planned for flagship
  paper C, not lead paper]

### H6 — Per-student LLM-tier composition outperforms uniform-tier deployment
- **State at 2026-04-30:** **NOT YET TESTED.** Implementation pending
  (tasks #12 + #14); cohort outcome data not yet collected.
- **Refinement after Prague + cohort 2:** [planned for empirical
  paper B, not lead paper]

### H7 — RAG-eligible content has natural tiers (academic / cohort-logistics / cultural)
- **State at 2026-04-30 (real-cohort metadata gathering):** Provisionally
  proposed. Direct observation of Ondrej's M E 336P announcements stream
  (cohort metadata 2026-04-30 entry) shows three content
  flavors mixing in one place: academic/professional (research-society
  opportunities), domain-cultural (a field-history map), and travel-logistics
  (restaurant recs, side trips, Czechia navigation). Treating these
  identically in retrieval degrades signal-to-noise on academic queries
  (a core-concept query probably shouldn't surface the restaurant-rec announcement,
  even if both share the cohort namespace).
- **Refinement after Prague:** [pending — measure whether per-tier RAG
  policies improve answer quality on the academic should-RAG-win battery
  while preserving access to logistics content for logistics queries]
- **Open design questions:**
  - How are tiers assigned? Auto-classify on ingest, or instructor-tag?
  - Are tiers per-Class (cohort-specific) or per-Course (canonical)?
  - Cross-tier retrieval policy: query-classification → tier-selection?

### Ledger-entry workflow

After each evidence checkpoint:
1. Add a sub-bullet under the relevant H* entry with date + checkpoint
   name + evidence summary
2. State refinement (reinforce / refine / retract)
3. If hypothesis is refuted, mark ❌ and capture in §B "Failed Hypotheses"
4. Cross-link to data artifact (round number, journey doc, LangFuse trace
   ID range)

This makes the paper auditable across its lifetime, not just at the
submission moment. Reviewers can see what we believed when, and why
the published claim is what it is.

---

## 10. Publication-readiness checklist

- [x] Synthetic corpus v1 (5 docs)
- [⚠] Synthetic corpus v2 (9 docs, expanded for stress-test) — round 4 in progress
- [x] Round 1-3 results (V1 vs V2 vs V3 chunkers, 18 questions)
- [⚠] Round 4 results (V2 vs V3 on expanded corpus, 26 questions) — in progress
- [ ] Inter-rater agreement (Ondrej + Booth + external)
- [ ] Multi-run variance (n≥3)
- [ ] Cross-LLM replication (add Llama 3 or Gemini)
- [ ] Public-benchmark replication (BEIR + SciFact applied to our rubric)
- [ ] Real cohort corpus run (post-Prague onboarding)
- [ ] IRB review (if cohort outcomes claimed)
- [ ] Indirect competitive framing pass on all wording
