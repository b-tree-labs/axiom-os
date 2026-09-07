# Axiom Product Metrics Framework

**Status:** Updated 2026-03-20 — aligned with Axiom platform architecture
**Owner:** Ben Booth
**Origin:** facility Digital Twin project (generalized to Axiom)
**Layer:** Axiom (product KPIs and business outcomes)

> **Scope of this document:** Product success metrics — business KPIs, user outcomes,
> domain-specific domain SLAs, and intelligence platform health as seen by users and operators.
>
> For *platform observability* (instrumentation, alerting, distributed traces, Prometheus
> metrics), see [spec-observability.md](spec-observability.md).
>
> These two documents are complementary. A metric like "RAG relevance score" appears in
> both: here as a product KPI (target >80%, measured by user feedback), and in
> `spec-observability.md` as `axiom_rag_low_confidence_ratio` (a real-time gauge that
> alerts if it crosses a threshold).

> **See also:** [Executive PRD Success Metrics](../requirements/prd-executive.md#success-metrics-platform-wide) for high-level targets.

---

## North Star Metrics

| Metric | Type | Rationale |
|--------|------|-----------|
| **Complete Operating Days** | Operational | # of days with full pipeline: raw data → simulation → visualization |
| **Weekly Engaged Users** | Adoption | Users who download data, run a query, or interact with an agent |

---

## 1. Reliability Metrics

*"Can users trust that the system works?"*

| Metric | Definition | Current | Target | Measurement |
|--------|------------|---------|--------|-------------|
| Pipeline Success Rate | % of nightly runs completing without error | Unknown | >98% | Cron exit codes; see `axiom_agent_runs_total` in [spec-observability.md](spec-observability.md) |
| Data Completeness | % of expected system operating days with processed data | Unknown | 100% | Box uploads vs DB records |
| Mean Time to Recovery | Hours from pipeline failure → restored operation | Unknown | <4 hrs | Incident log timestamps |
| Data Integrity Score | % of records passing validation checks | Unknown | >99.5% | Automated QA scripts |

---

## 2. Latency Metrics

*"How fast do users get value?"*

| Metric | Definition | Current | Target (Near) | Target (Future) | Measurement |
|--------|------------|---------|---------------|-----------------|-------------|
| Raw Data Latency | Hours from system event → Box upload | Unknown | <8 hrs | <1 hr | Timestamp comparison |
| Processing Latency | Hours from Box upload → DB available | ~24 hrs | <12 hrs | <1 hr | Pipeline timestamps |
| Visualization Latency | Hours from DB update → Plotly regenerated | ~24 hrs | <12 hrs | Real-time | File mod times |
| Query Response Time | Seconds for agent to return a grounded answer | Unknown | <5 sec | <2 sec | `axiom_llm_latency_seconds` p95 |
| Simulation Turnaround | Hours from state detection → physics-code results | Unknown | <6 hrs | <1 hr | SLURM timestamps |

---

## 3. Adoption Metrics

*"Are people actually using this?"*

| Metric | Definition | Current | Target | Measurement |
|--------|------------|---------|--------|-------------|
| Weekly Active Users | Unique users per week | Unknown | Baseline +25%/semester | Web server logs |
| Feature Utilization | % users engaging each feature | Unknown | >50% use 2+ features | Click tracking |
| Return Rate | % users returning within 30 days | Unknown | >60% | Session analytics |
| Data Downloads | CSV/HDF5 exports per month | Unknown | Track growth | Download logs |
| Session Duration | Average minutes per visit | Unknown | >5 min | Analytics |
| Support Requests | Questions/issues per month | Unknown | Track & categorize | Email/Slack |

---

## 4. Accuracy Metrics

*"Can users trust the outputs?"*

| Metric | Definition | Current | Target | Measurement |
|--------|------------|---------|--------|-------------|
| Critical Rod Height RMSE | Predicted vs measured (cm) | Unknown | <1.0 cm | Comparison script |
| Bias Correction Stability | Std dev of calibration coefficients | Unknown | <5% drift/quarter | Statistical tracking |
| Change Point Precision | % detected states confirmed by operators | Unknown | >90% | Operator review |
| Text-to-SQL Accuracy | % queries returning correct results | Unknown | >85% | Human evaluation |
| RAG Relevance Score | % retrieved docs rated relevant by users | Unknown | >80% | Thumbs up/down; see §5 for intelligence platform detail |
| Knowledge Maturity Progression | % of interactions contributing to validated facts | Unknown | >5% after 6 months | Crystallization pipeline; see §5 |

---

## 5. Intelligence Platform Metrics

*"Is the AI layer delivering compounding value?"*

These metrics are specific to the Axiom intelligence layer (RAG, LLM gateway, knowledge
maturity pipeline, prompt registry). They track whether the platform improves over time,
not just whether it responds correctly today.

For the underlying instrumentation that feeds these metrics, see
[spec-observability.md §2](spec-observability.md).

| Metric | Definition | Current | Target | Measurement |
|--------|------------|---------|--------|-------------|
| RAG Relevance Score | % of retrieved document sets rated relevant by users | Unknown | >80% | In-session thumbs up/down; `axiom_knowledge_feedback_signals` |
| Knowledge Maturity Rate | % of interactions that produce at least one validated fact | Unknown | >5% after 6 months | `axiom_knowledge_crystallized_total` / `axiom_knowledge_interactions_total` |
| Prompt Cache Hit Ratio | % of LLM calls where static template blocks hit the provider's prompt cache | Unknown | >60% after warm-up | `axiom_prompt_cache_hint_ratio` |
| Agentic RAG Efficiency | % of queries resolved without a second retrieval pass | Unknown | >70% | Retrieval loop count per `trace_id` |
| Crystallization Throughput | Facts approved per week | Unknown | Track growth; positive slope is success | `axiom_knowledge_facts_approved_total` weekly delta |
| Correction Rate | % of interactions with explicit negative feedback (thumbs-down or correction) | Unknown | <10%; alert if >10% sustained | `axiom_knowledge_feedback_signals{signal_type="negative"}` / total |

### Intelligence Platform Notes

- **Knowledge maturity rate** is the key long-horizon signal. A platform that learns
  from every interaction should show a steadily rising crystallization throughput. Flat
  or declining throughput indicates the review queue is a bottleneck (see
  `axiom_knowledge_facts_pending_review` alert in [spec-observability.md §3](spec-observability.md)).
- **Correction rate** above 10% is both a quality alert and a training signal. TRIAGE
  should surface it in `axiom doctor`; it also feeds back into RAG corpus curation.
- **Prompt cache hit ratio** has a direct cost impact: cached input tokens are billed
  at a fraction of uncached tokens on most providers. The 60% target is achievable after
  a warm-up period once templates are stable.
- None of these metrics require changing user behaviour. All are derived from existing
  interaction log events and the feedback signals already flowing through the SCAN pipeline.

---

## 6. Compliance Metrics

*"Are we inspection-ready?"*

| Metric | Definition | Current | Target | Measurement |
|--------|------------|---------|--------|-------------|
| Documentation Coverage | % operating days with complete audit trail | Unknown | 100% | Gap analysis script against `audit_events.jsonl` |
| Record Retention | Years of historical data accessible | Unknown | >10 years | Storage inventory |
| Traceability Score | % predictions linkable to source + code version | Unknown | 100% | `trace_id` coverage check; see [spec-observability.md §4](spec-observability.md) |
| Report Generation Time | Minutes to produce compliance report | Unknown | <5 min | `axiom doctor --metrics` timing |
| Anomaly Detection Rate | % off-normal conditions flagged | 0% | >80% | Compare vs operator logs |
| Surveillance Check Compliance | % of required periodic checks completed on time | Unknown | 100% | Schedule vs completion timestamps |

---

## 7. Education Metrics

*"Are students learning?"*

| Metric | Definition | Current | Target | Measurement |
|--------|------------|---------|--------|-------------|
| Simulator Sessions | Monthly uses of PKE Simulator | Unknown | >100/semester | Application logs |
| Learning Gain | Pre/post assessment improvement | Not measured | >20% | Quiz administration |
| Instructor Adoption | # courses incorporating tools (target: M E 390G, M E 361E, M E 336P) | Unknown | 2+ courses | Faculty survey |
| Student Satisfaction | NPS score | Not measured | >40 | Survey |
| Time to Competency | Simulator hours before passing practical | Not measured | Establish baseline | Correlation analysis |

---

## 8. Medical Isotope Production Metrics

*"Can we deliver isotopes reliably and quickly?"*

| Metric | Definition | Current | Target | Measurement |
|--------|------------|---------|--------|-------------|
| Request-to-Package Time | Hours from isotope order → ops package delivered | Unknown (hours?) | <30 min | Timestamp tracking |
| Request-to-Irradiation Time | Hours from order → system begins production | Unknown | <24 hrs | Schedule tracking |
| Production Success Rate | % of isotope runs meeting activity spec | Unknown | >99% | Production logs |
| SLA Compliance | % of orders delivered within promised window | Not tracked | >95% | Order tracking system |
| Manual Intervention Rate | % of orders requiring phone calls/emails | ~100% | <10% | Workflow audit |
| Ops Package Accuracy | % of auto-generated packages accepted without changes | 0% (manual today) | >90% | Operator feedback |

### Current State (As-Is)

1. Hospital calls the isotope coordinator directly with isotope request
2. The isotope coordinator assigns to available student/staff
3. Assignee manually reviews recent ops, runs simulations
4. Criticality data compiled into Word doc with bullet points
5. Word doc delivered via phone call to operators next morning
6. No SLA tracking, no visibility into process

### Ideal State (To-Be)

1. Hospital submits request via portal (or API integration)
2. System auto-checks system schedule availability
3. facility DT generates simulation package automatically
4. Ops package delivered to operators digitally with checklist
5. Production tracked with SLA visibility
6. Post-production verification and delivery confirmation

---

## 9. Commercialization Readiness Metrics

*"Can we replicate and sell this?"*

| Metric | Definition | Current | Target | Measurement |
|--------|------------|---------|--------|-------------|
| Deployment Time | Hours to stand up for new system | Unknown | <40 hrs | Time tracking |
| Infrastructure Cost | Monthly $ for compute + storage | Unknown | Document baseline | PrivateCloud billing |
| Code Portability | % codebase without PrivateCloud dependencies | Unknown | >80% | Dependency audit |
| Documentation Completeness | % components with install docs | ~30% | 100% | Doc inventory |
| External Interest | Inbound inquiries from facilities | Unknown | Track all | CRM/email |
| Pilot Candidates | Facilities expressing pilot interest | 0 | 2+ | Outreach tracking |
| **Isotope Revenue Potential** | Est. annual $ if isotope production monetized | $0 | Document model | Market research |
| **Isotope-Capable Systems** | # of US research systems that could produce medical isotopes | ~30 | Identify 5 pilots | Industry survey |

---

## 10. Metrics Collection Infrastructure

For instrumentation, alerting, trace correlation, and the Prometheus/OTel collection
stack, see [spec-observability.md](spec-observability.md). The table below captures
the product-level collection gaps — what data is still missing to populate the metrics
in §1–§9.

| Gap | Current State | Required | Observability hook |
|-----|---------------|----------|--------------------|
| Pipeline observability | Cron logs only | Structured logging, alerting | `axiom_agent_runs_total` |
| User analytics | None | Web analytics (Plausible, PostHog) | Session/click tracking |
| Accuracy tracking | Manual spot checks | Automated comparison jobs → metrics DB | `axiom_rag_low_confidence_ratio` |
| User feedback | None | In-app feedback buttons (thumbs up/down) | `axiom_knowledge_feedback_signals` |
| Cost tracking | Unknown | PrivateCloud usage reports, monthly review | `axiom_llm_tokens_total` |
| Compliance audit coverage | Manual | `trace_id` on all log records | [spec-observability.md §4](spec-observability.md) |

---

## 11. Immediate Actions

1. **Instrument the pipeline** — Add `trace_id` to all structured log records (Phase 1 of [spec-observability.md §7](spec-observability.md))
2. **Add in-session feedback** — Deploy thumbs up/down to populate `axiom_knowledge_feedback_signals` and enable Correction Rate tracking
3. **Add web analytics** — Deploy lightweight analytics for adoption and session metrics
4. **Document current costs** — Get PrivateCloud billing data for infrastructure baseline
5. **Define "operating day"** — Agree on unit of measurement for pipeline completeness
6. **Run `axiom doctor --metrics`** — Establish baseline against all alert thresholds in [spec-observability.md §3](spec-observability.md)
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
