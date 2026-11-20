# Design Loop Architecture

**The Product Development Feedback Loop for Axiom**

| Property | Value |
|----------|-------|
| Status | Conceptual — not yet instrumented |
| Implementation | `axiom signal` implements the SENSE stage; other stages are manual |
| Related Specs | [Agent Architecture](spec-agent-architecture.md), [CLI Spec](spec-axi-cli.md) |

> **Note:** "Sense" in this document refers to the product development loop stage, not the CLI command. The CLI command implementing this stage is `axiom signal` (provided by the `signal_agent` extension).

## Vision

Build the most adapted and finely tuned operating system by:
1. Building a continuous Sense → Synthesize → Create → Publish → Sense loop
2. Measuring loop health metrics
3. Increasing feedback loop velocity over time

## The Loop

```
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                         │
│    ┌──────────┐      ┌─────────────┐      ┌──────────┐      ┌─────────┐│
│    │  SENSE   │ ──── │  SYNTHESIZE │ ──── │  CREATE  │ ──── │ PUBLISH ││
│    └──────────┘      └─────────────┘      └──────────┘      └─────────┘│
│         ▲                                                        │     │
│         │                                                        │     │
│         │                    USER FEEDBACK                       │     │
│         └────────────────────────────────────────────────────────┘     │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 1. SENSE — Signal Extraction

**What:** Extract signals from all sources
**Who:** Product, Design, Build (humans + agents)
**Sources:**
- Calendar, notes, voice memos, Slack, email
- User feedback (usability studies, support tickets, analytics)
- Code changes (GitLab diffs, PRs, issues)
- Design reviews, iteration feedback
- Operational data (logs, metrics, alerts)

**Outputs:** Raw signals with metadata (source, timestamp, people, type)

### 2. SYNTHESIZE — Pattern Recognition & Actionable Outputs

**What:** Cluster signals, generate actionable artifacts
**Who:** Synthesis agents, Product
**Activities:**
- Cluster signals by PRD/initiative
- Generate PRD updates (requirements, decisions, questions)
- Generate design briefs for designers
- Generate issue drafts for builders
- Generate stakeholder briefings

**Outputs:**
- Updated PRDs
- Design briefs
- Issue drafts
- Briefing narratives

### 3. CREATE — Design & Development

**What:** Produce hardened designs and implementations
**Who:** Designers, Design agents, Builders

**Designers Subscribe To:**
- PRD updates (new requirements, changed scope)
- Early iteration feedback signals
- User feedback on existing designs

**Activities:**
- Combine PRD requirements with sensed feedback
- Iterate on designs with early feedback
- Produce hardened designs (wireframes, specs, prototypes)
- Review with stakeholders

**Outputs:**
- Design specifications
- Wireframes / mockups
- Prototypes
- Implementation-ready specs

### 4. PUBLISH — Delivery to Builders & Users

**What:** Ship designs to builders, features to users
**Who:** Build agents, Builders, DevOps

**Activities:**
- Publish design specs to builders
- Implement features
- Deploy to users
- Instrument for feedback collection

**Outputs:**
- Shipped features
- Instrumented user touchpoints
- Feedback collection mechanisms

### 5. SENSE (again) — User Feedback Loop

**What:** Sense user feedback on shipped features
**Who:** Product, Design, Build (all sensing)
**Signals:**
- **Usefulness:** Does it solve the problem?
- **Ease of use:** Is it intuitive?
- **Joy to use:** Is it delightful?
- **Performance:** Is it fast/reliable?
- **Completeness:** What's missing?

**Sources:**
- User interviews / usability studies
- Support tickets / bug reports
- Analytics (usage patterns, drop-offs)
- NPS / satisfaction surveys
- Social / community feedback

**Outputs:** New signals → feed back into SENSE

---

## Roles & Subscriptions

### Product (Requirements)
```
Subscribes to:
  - User feedback signals
  - Design iteration feedback
  - Build blockers/questions
  - Market/competitive signals

Publishes:
  - PRD updates
  - Prioritization decisions
  - Scope changes
```

### Design (Create)
```
Subscribes to:
  - PRD updates
  - Early iteration feedback
  - User feedback (ease, joy)
  - Build feasibility feedback

Publishes:
  - Design specs
  - Prototypes
  - Design decisions
```

### Build (Implement)
```
Subscribes to:
  - Design specs
  - PRD clarifications
  - User feedback (performance, bugs)

Publishes:
  - Shipped features
  - Technical decisions
  - Blockers/questions
```

---

## Loop Health Metrics

### Velocity Metrics
| Metric | Description | Target |
|--------|-------------|--------|
| **Cycle Time** | Signal sensed → feature shipped | Decreasing |
| **Sense Latency** | Event occurs → signal captured | < 24 hours |
| **Synthesis Latency** | Signal captured → actionable output | < 4 hours |
| **Create Latency** | Brief received → design complete | Tracked |
| **Publish Latency** | Design approved → shipped | Tracked |
| **Feedback Latency** | Feature shipped → feedback sensed | < 7 days |

### Quality Metrics
| Metric | Description | Target |
|--------|-------------|--------|
| **Signal Quality** | Average confidence of extracted signals | > 0.7 |
| **Synthesis Accuracy** | % of drafts approved without major edits | > 80% |
| **Design Hit Rate** | % of designs that ship without major rework | > 70% |
| **User Satisfaction** | Post-ship feedback (usefulness + ease + joy) | Increasing |
| **Rework Rate** | % of features requiring post-ship fixes | Decreasing |

### Throughput Metrics
| Metric | Description | Target |
|--------|-------------|--------|
| **Signals/Day** | Raw signals captured daily | Tracked |
| **Synthesis/Day** | PRD updates, briefs generated | Tracked |
| **Designs/Week** | Design specs completed | Tracked |
| **Features/Sprint** | Features shipped per sprint | Tracked |

### Health Dashboard
```
Loop Health Score = f(
  velocity_improvement,
  quality_scores,
  throughput_trend,
  feedback_loop_closure_rate
)

Target: Loop Health Score improving quarter-over-quarter
```

---

## Data Model Extensions

### Signal (extended)
```python
@dataclass
class Signal:
    # ... existing fields ...
    
    # Loop context
    loop_stage: str  # sense, synthesize, create, publish, feedback
    feedback_type: str | None  # usefulness, ease, joy, performance, completeness
    feature_ref: str | None  # Link to feature being discussed
    design_ref: str | None  # Link to design being discussed
    
    # Loop metrics
    sensed_at: str  # When signal was captured
    source_event_at: str | None  # When underlying event occurred (if known)
```

### LoopIteration
```python
@dataclass
class LoopIteration:
    """Tracks one complete loop cycle for a feature/initiative."""
    iteration_id: str
    initiative: str
    
    # Stage timestamps
    initial_sense_at: str | None
    synthesis_at: str | None
    design_complete_at: str | None
    published_at: str | None
    feedback_sensed_at: str | None
    
    # Metrics
    cycle_time_days: float | None
    quality_score: float | None
    rework_count: int
    
    # Artifacts
    prd_updates: list[str]  # draft IDs
    design_refs: list[str]
    feature_refs: list[str]
    feedback_signals: list[str]  # signal IDs
```

### Subscription
```python
@dataclass
class Subscription:
    """Who subscribes to what."""
    subscriber_id: str  # person or agent
    subscriber_role: str  # product, design, build
    artifact_type: str  # prd, design_brief, design_spec, issue
    initiative_filter: list[str] | None  # specific PRDs/initiatives
    signal_type_filter: list[str] | None  # specific signal types
    delivery_method: str  # email, slack, feed, webhook
```

---

## Implementation Phases

### Phase 0: MVP (Current)
- Sense: Calendar, notes extractors
- Synthesize: PRD updates, briefings
- Manual create/publish
- Manual feedback collection

### Phase 1: Designer Loop
- Add design brief synthesis
- Designer subscription system
- Design artifact tracking
- Early feedback sensing

### Phase 2: Builder Loop
- Issue generation from designs
- Builder subscription system
- Ship tracking
- Automated instrumentation hints

### Phase 3: User Feedback Loop
- User feedback extractors (surveys, analytics, support)
- Feedback → signal → PRD automation
- Loop closure detection
- Automated loop health dashboard

### Phase 4: Loop Optimization
- ML-based signal routing
- Automated bottleneck detection
- Velocity optimization recommendations
- Cross-initiative learning

---

## Example Flow

```
Day 1: User complains about confusing experiment setup (SENSE)
        ↓
Day 1: Signal extracted: "Experiment setup UX confusion" → experiment_manager
        ↓
Day 2: Synthesized into design brief for UX improvement (SYNTHESIZE)
        ↓
Day 3: Designer receives brief, starts iteration (CREATE)
        ↓
Day 5: Early prototype shared, feedback sensed (SENSE mini-loop)
        ↓
Day 7: Hardened design published to builders (PUBLISH)
        ↓
Day 10: Feature shipped
        ↓
Day 14: User feedback sensed: "Much clearer now!" (SENSE)
        ↓
Loop Closed. Cycle Time: 14 days. Quality: High.
```

---

## Success Criteria

**Year 1:**
- Loop fully instrumented
- Baseline metrics established
- Manual loop closure < 30 days average

**Year 2:**
- 50% reduction in cycle time
- Automated synthesis for 80% of signals
- Designer/builder subscriptions active

**Year 3:**
- Continuous improvement loop self-optimizing
- < 14 day average cycle time
- User feedback integrated within 48 hours

---

## Open Questions

1. How do we handle conflicting feedback signals?
2. What's the right granularity for loop tracking (feature vs. initiative)?
3. How do agents participate in create/publish stages?
4. How do we measure "joy to use" quantitatively?
5. When does a loop "close" vs. iterate?
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
