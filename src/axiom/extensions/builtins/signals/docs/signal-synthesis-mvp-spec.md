# Neut Sense & Synthesis MVP (Phase 0) Specification

**Version:** 0.1.0  
**Date:** February 24, 2026  
**Author:** Ben Booth  
**Status:** Ready for Implementation  
**Target:** 1-week sprint

---

## Objective

Process Ben's two-week backlog of voice memos, Teams transcripts, calendar events, and personal notes to:
1. Extract actionable signals from all sources
2. Synthesize updates to four PRDs: Ops Log, Experiment Manager, Operator Dashboard, Researcher Dashboard
3. Generate narratives for design kickoff briefings

**Success Criteria:**
- [ ] All backlog items ingested and processed
- [ ] 4 PRD document sections updated with requirements extracted from signals
- [ ] Design-ready briefing narrative for each module
- [ ] Pipeline repeatable for ongoing use

---

## Scope

### In Scope (Phase 0)
- Voice memo transcription + signal extraction
- Teams transcript parsing + signal extraction  
- Calendar event extraction (meetings, deadlines)
- Manual note ingestion (markdown files)
- Signal clustering by PRD/initiative
- Document section synthesis (PRD updates)
- Narrative synthesis (design briefings)
- CLI-driven workflow
- Local file storage (JSON)

### Out of Scope (Future Phases)
- Real-time feed/dashboard UI
- Federation (cross-org)
- OpenFGA authorization (single user for now)
- Email/social media targets
- Auto-curation confidence thresholds
- Override tracking and learning

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         PHASE 0 MVP                                  │
│                                                                      │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐          │
│  │   INGEST     │───▶│   EXTRACT    │───▶│   CLUSTER    │          │
│  │              │    │              │    │              │          │
│  │  voice/      │    │  Whisper     │    │  By PRD      │          │
│  │  teams/      │    │  + LLM       │    │  By Person   │          │
│  │  calendar/   │    │              │    │  By Theme    │          │
│  │  notes/      │    │              │    │              │          │
│  └──────────────┘    └──────────────┘    └──────────────┘          │
│                                                 │                    │
│                                    ┌────────────▼────────────┐      │
│                                    │      SYNTHESIZE         │      │
│                                    │                         │      │
│                                    │  ┌─────────────────┐   │      │
│                                    │  │ PRD Section     │   │      │
│                                    │  │ Updates         │   │      │
│                                    │  └─────────────────┘   │      │
│                                    │  ┌─────────────────┐   │      │
│                                    │  │ Design Briefing │   │      │
│                                    │  │ Narratives      │   │      │
│                                    │  └─────────────────┘   │      │
│                                    └────────────────────────┘      │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Data Sources

### 1. Voice Memos
**Location:** `tools/agents/inbox/raw/voice/`  
**Format:** .m4a, .mp3, .wav  
**Processing:** Whisper transcription → LLM signal extraction

### 2. Teams Transcripts  
**Location:** `tools/agents/inbox/raw/teams/`  
**Format:** .vtt, .docx (exported from Teams)  
**Processing:** Parse speakers/timestamps → LLM signal extraction

### 3. Calendar Events
**Location:** Google Calendar API or exported .ics  
**Format:** iCalendar  
**Processing:** Extract meetings with attendees, parse for initiative keywords

### 4. Personal Notes
**Location:** `tools/agents/inbox/raw/notes/`  
**Format:** .md, .txt  
**Processing:** Direct LLM signal extraction

---

## Signal Model (Simplified)

```python
@dataclass
class Signal:
    signal_id: str              # SHA256 hash
    source: str                 # voice, teams, calendar, notes
    timestamp: str              # ISO 8601
    raw_text: str               # Original content
    signal_type: str            # requirement, decision, question, insight, action_item
    prd_target: str | None      # ops_log, experiment_manager, operator_dashboard, researcher_dashboard
    people: list[str]           # Mentioned people
    detail: str                 # LLM-extracted summary
    confidence: float           # 0.0-1.0
    
    # Metadata
    source_file: str
    extracted_at: str
```

### Signal Types for PRD Work

| Type | Description | Example |
|------|-------------|---------|
| `requirement` | User need or feature request | "Operators need to see last 30 min of status checks" |
| `decision` | Design or scope decision made | "We'll use append-only log entries, no edits" |
| `question` | Open question needing resolution | "Should experiment log be separate from ops log?" |
| `insight` | Stakeholder context or constraint | "Jim mentioned regulations require 2-year retention" |
| `action_item` | Task to complete | "Schedule follow-up with Nick on sample tracking" |

---

## Target PRDs

### 1. Ops Log PRD
**File:** `docs/prds/prd-ops-log.md`  
**Section to Update:** `## Requirements` → subsections for new requirements  
**Stakeholders in Signals:** Jim (TJ), Nick Luciano  
**Key Topics:** Console checks, shift handoffs, tamper-proof entries, compliance

### 2. Experiment Manager PRD
**File:** `docs/prds/prd-experiment-manager.md`  
**Section to Update:** `## Requirements`  
**Stakeholders in Signals:** Khiloni Shah, Nick Luciano  
**Key Topics:** Sample tracking, lifecycle states, supervisor approval, chain of custody

### 3. Operator Dashboard (NEW)
**File:** `docs/prds/prd_operator-dashboard.md` (create if not exists)  
**Purpose:** Real-time view for operators during shifts  
**Key Topics:** Compliance status, upcoming checks, recent events, alerts

### 4. Researcher Dashboard (NEW)  
**File:** `docs/prds/prd_researcher-dashboard.md` (create if not exists)  
**Purpose:** Experiment status and sample tracking for researchers  
**Key Topics:** My experiments, sample locations, upcoming experiments, results

---

## Synthesis Targets

### Target 1: PRD Section Updates

**For each PRD, synthesize:**

```markdown
## Requirements Extracted from Stakeholder Input
<!-- Auto-generated by neut signal on {date} -->
<!-- Source signals: {count} from voice memos, Teams, notes -->

### New Requirements

#### REQ-XXX: {Title}
**Source:** {stakeholder} via {source_type} on {date}
**Type:** Functional | Non-functional | Constraint
**Priority:** P0 | P1 | P2

{Requirement description synthesized from signals}

**Rationale:** {Why this matters, from signal context}

**Open Questions:**
- {Any unresolved questions from signals}
```

### Target 2: Design Briefing Narratives

**For each module, generate 750-1200 word narrative:**

```markdown
# Design Briefing: {Module Name}

## Context
{What this module does, who it's for}

## What We Learned (Past 2 Weeks)
{Synthesized insights from signals, organized by theme}

### From Stakeholders
{Key quotes and requirements attributed to people}

### Decisions Made
{Any design decisions captured in signals}

### Open Questions
{Questions that need resolution before design}

## Recommended Next Steps
{Prioritized list for design kickoff}

## Source Signals
{List of signal_ids for traceability}
```

---

## CLI Commands

```bash
# ═══════════════════════════════════════════════════════════════════════
# INGEST
# ═══════════════════════════════════════════════════════════════════════

# Ingest all sources in inbox
neut signal ingest --all

# Ingest specific source
neut signal ingest --source voice
neut signal ingest --source teams
neut signal ingest --source calendar --since 2026-02-10
neut signal ingest --source notes

# ═══════════════════════════════════════════════════════════════════════
# REVIEW SIGNALS
# ═══════════════════════════════════════════════════════════════════════

# List extracted signals
neut signal signals list
neut signal signals list --prd ops_log
neut signal signals list --type requirement
neut signal signals list --person "Jim"

# Show signal detail
neut signal signals show sig_abc123

# Manually tag/correct signal
neut signal signals tag sig_abc123 --prd experiment_manager
neut signal signals tag sig_abc123 --type decision

# ═══════════════════════════════════════════════════════════════════════
# CLUSTER
# ═══════════════════════════════════════════════════════════════════════

# Cluster signals by PRD
neut signal cluster --by prd

# Show clusters
neut signal cluster list
neut signal cluster show cluster_ops_log

# ═══════════════════════════════════════════════════════════════════════
# SYNTHESIZE
# ═══════════════════════════════════════════════════════════════════════

# Synthesize PRD updates
neut signal synthesize prd ops_log --preview
neut signal synthesize prd ops_log --apply

# Synthesize all PRDs
neut signal synthesize prd --all --preview

# Generate design briefing narrative
neut signal synthesize briefing ops_log --output docs/design/briefings/
neut signal synthesize briefing --all

# ═══════════════════════════════════════════════════════════════════════
# STATUS
# ═══════════════════════════════════════════════════════════════════════

# Overall status
neut signal status

# Output:
# Inbox:
#   voice/: 23 files (18 processed, 5 pending)
#   teams/: 4 files (4 processed)
#   notes/: 12 files (10 processed, 2 pending)
# 
# Signals: 142 total
#   - ops_log: 34
#   - experiment_manager: 28
#   - operator_dashboard: 15
#   - researcher_dashboard: 12
#   - unassigned: 53
#
# Synthesis:
#   - ops_log PRD: Updated 2 hours ago (34 signals)
#   - experiment_manager PRD: Pending (28 signals)
```

---

## File Structure (Phase 0)

```
tools/pipelines/sense/
├── __init__.py
├── cli.py                      # CLI entry point (extend existing)
├── models.py                   # Signal dataclass (extend existing)
│
├── extractors/
│   ├── __init__.py
│   ├── voice.py                # EXISTS - Whisper transcription
│   ├── teams.py                # EXISTS - Teams transcript parsing
│   ├── calendar.py             # NEW - iCal/Google Calendar
│   └── notes.py                # NEW - Markdown/text notes
│
├── clustering/
│   ├── __init__.py
│   └── prd_clusterer.py        # NEW - Cluster by PRD target
│
├── synthesis/
│   ├── __init__.py
│   ├── prd_updater.py          # NEW - PRD section synthesis
│   └── briefing_generator.py   # NEW - Design briefing narratives
│
├── prompts/
│   ├── __init__.py
│   ├── extraction_v1.py        # Signal extraction prompts
│   ├── prd_synthesis_v1.py     # NEW - PRD update prompts
│   └── briefing_v1.py          # NEW - Briefing narrative prompts
│
└── inbox/                      # Data directory
    ├── raw/
    │   ├── voice/              # Input: .m4a, .mp3
    │   ├── teams/              # Input: .vtt, .docx
    │   ├── calendar/           # Input: .ics or API cache
    │   └── notes/              # Input: .md, .txt
    ├── processed/
    │   ├── transcripts/        # Whisper output
    │   └── signals/            # Extracted signals JSON
    └── output/
        ├── prd_updates/        # Generated PRD sections
        └── briefings/          # Generated narratives
```

---

## Prompts

### Signal Extraction Prompt

```python
SIGNAL_EXTRACTION_PROMPT = """
You are extracting actionable signals from meeting notes, voice memos, and transcripts 
for a operations platform project (Axiom).

Target PRDs (tag signals to the most relevant):
- ops_log: Operations logging, console checks, shift handoffs, compliance
- experiment_manager: Sample tracking, experiment lifecycle, supervisor approval, chain of custody
- operator_dashboard: Real-time operator view, compliance status, alerts
- researcher_dashboard: Researcher experiment view, sample status, results

Signal Types:
- requirement: A user need or feature request
- decision: A design or scope decision that was made
- question: An open question needing resolution
- insight: Stakeholder context, constraint, or background
- action_item: A task that needs to be done

Known Stakeholders:
- Jim (TJ): Operations Manager, compliance expert
- Nick Luciano: Senior Operator, daily ops perspective
- Khiloni Shah: Graduate researcher, experiment workflow
- Kevin Clarno: Program lead, cross-project visibility
- Ben Booth: Product owner, technical lead

Extract signals as JSON array:
```json
[
  {
    "signal_type": "requirement",
    "prd_target": "ops_log",
    "people": ["Jim"],
    "detail": "Operators need to see gap alerts if console check is >35 minutes overdue",
    "raw_quote": "Jim mentioned that 30 minutes is the rule but we should alert at 35...",
    "confidence": 0.85
  }
]
```

Only extract signals that are actionable or informative for the PRDs.
Skip small talk, tangents, and non-product discussions.
Attribute to people when clearly identifiable.

Transcript to process:
{transcript}
"""
```

### PRD Section Synthesis Prompt

```python
PRD_SECTION_PROMPT = """
You are updating a Product Requirements Document (PRD) for Axiom, 
a digital operations platform.

PRD: {prd_name}
Existing Requirements Section:
{existing_section}

New signals to incorporate:
{signals_json}

Generate a Markdown section that:
1. Lists new requirements extracted from signals
2. Uses format: REQ-{prd_code}-XXX (e.g., REQ-OPS-042)
3. Attributes each requirement to its source (person, date, signal type)
4. Notes any open questions or conflicts
5. Does NOT duplicate existing requirements
6. Preserves any existing requirement numbers

Output format:
```markdown
### Requirements from Stakeholder Input (Auto-extracted {date})

#### REQ-{code}-XXX: {Title}
**Source:** {person} via {source} ({date})  
**Type:** {Functional|Non-functional|Constraint}  
**Priority:** {P0|P1|P2} (based on signal context)

{Description}

**Rationale:** {Why this matters}

**Open Questions:**
- {Any unresolved items}

---
```

Be specific. Use exact quotes where helpful. Don't invent requirements not in signals.
"""
```

### Design Briefing Prompt

```python
BRIEFING_PROMPT = """
You are writing a design briefing document to kick off UI/UX design work 
for a Axiom module.

Module: {module_name}
Purpose: {module_purpose}
Target Users: {user_personas}

Signals from past 2 weeks:
{signals_json}

Write a 750-1200 word briefing narrative that:

1. **Context** (100 words): What this module does, who uses it, why it matters
2. **What We Learned** (400-600 words): 
   - Key insights from stakeholders (attribute by name)
   - Requirements that emerged
   - Constraints discovered (regulatory, technical, workflow)
   - Use direct quotes where impactful
3. **Decisions Made** (100 words): Any design decisions already locked in
4. **Open Questions** (100 words): Questions for design to resolve
5. **Recommended Next Steps** (100 words): Prioritized actions

Tone: Clear, direct, actionable. Write for a designer who hasn't been in the meetings.

End with a "Source Signals" section listing signal IDs for traceability.
"""
```

---

## Implementation Steps

### Day 1: Ingest Pipeline
- [ ] Verify voice.py Whisper transcription works for backlog
- [ ] Verify teams.py parses .vtt/.docx transcripts
- [ ] Implement calendar.py for .ics or Google Calendar API
- [ ] Implement notes.py for markdown ingestion
- [ ] Test: `neut signal ingest --all` processes backlog

### Day 2: Signal Extraction
- [ ] Refine extraction prompt with PRD targets
- [ ] Add `prd_target` field to Signal model
- [ ] Add signal tagging CLI commands
- [ ] Test: Signals correctly tagged to PRDs

### Day 3: Clustering
- [ ] Implement prd_clusterer.py
- [ ] Group signals by PRD, then by theme within PRD
- [ ] CLI: `neut signal cluster --by prd`

### Day 4: PRD Synthesis
- [ ] Implement prd_updater.py
- [ ] Read existing PRD, inject new requirements section
- [ ] Test on ops_log PRD first
- [ ] CLI: `neut signal synthesize prd ops_log --preview`

### Day 5: Briefing Synthesis
- [ ] Implement briefing_generator.py
- [ ] Generate narratives for all 4 modules
- [ ] Output to docs/design/briefings/
- [ ] Review and iterate on prompt

### Day 6: Polish & Run
- [ ] Process full backlog end-to-end
- [ ] Manual review of PRD updates
- [ ] Manual review of briefings
- [ ] Fix any issues, re-run

### Day 7: Handoff
- [ ] PRD sections merged into main PRDs
- [ ] Briefings ready for design review
- [ ] Document lessons learned for Phase 1

---

## Success Metrics

| Metric | Target |
|--------|--------|
| Backlog items processed | 100% |
| Signals extracted | >100 |
| Signals tagged to PRDs | >70% |
| PRD sections generated | 4 |
| Briefing narratives generated | 4 |
| Manual corrections needed | <20% of signals |

---

## Dependencies

### Existing Code (Leverage)
- `tools/pipelines/sense/extractors/voice.py` — Whisper transcription
- `tools/pipelines/sense/extractors/teams.py` — Teams parsing  
- `tools/pipelines/sense/models.py` — Signal dataclass
- `tools/pipelines/sense/cli.py` — CLI framework

### External
- OpenAI Whisper API or local whisper.cpp
- Anthropic Claude API for extraction/synthesis
- Python 3.11+

### New Files to Create
- `tools/pipelines/sense/extractors/calendar.py`
- `tools/pipelines/sense/extractors/notes.py`
- `tools/pipelines/sense/clustering/prd_clusterer.py`
- `tools/pipelines/sense/synthesis/prd_updater.py`
- `tools/pipelines/sense/synthesis/briefing_generator.py`
- `tools/pipelines/sense/prompts/prd_synthesis_v1.py`
- `tools/pipelines/sense/prompts/briefing_v1.py`

---

## Post-Phase 0: Transition to Phase 1

After Phase 0 success:
1. Add feed_post target for ongoing signal visibility
2. Add quality scoring to filter low-value signals
3. Add trust tiers for synthesis approval
4. Build basic web dashboard for signal review
5. Implement override tracking for continuous improvement

---

## Appendix A: Backlog Inventory

*To be filled in with actual file counts:*

| Source | Location | Count | Date Range |
|--------|----------|-------|------------|
| Voice Memos | inbox/raw/voice/ | ~25 | Feb 10-24, 2026 |
| Teams Transcripts | inbox/raw/teams/ | ~6 | Feb 10-24, 2026 |
| Calendar | Google Calendar | ~30 meetings | Feb 10-24, 2026 |
| Notes | inbox/raw/notes/ | ~15 | Feb 10-24, 2026 |

---

## Appendix B: Full Capstone Vision Reference

This MVP is Phase 0 of the full Sense & Synthesis Platform. The complete vision includes:

- **Multi-target synthesis:** Documents, spreadsheets, presentations, emails, calendar events, social media
- **LLM-first curation:** Auto-publish with human override as exception
- **Viewer personalization:** Same signals synthesized differently per user role
- **Federation:** Cross-org signal sharing with OpenFGA authorization (RBAC + ReBAC + ABAC)
- **Federated learning:** Privacy-preserving model improvement across organizations
- **Management dashboard:** Users control their information ecosystem

See: `docs/specs/sense-synthesis-capstone-spec.md` (to be created after Phase 0)

---

*This spec is designed to be self-contained for implementation. Claude should be able to implement each component given this context plus the existing sense module code.*
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
