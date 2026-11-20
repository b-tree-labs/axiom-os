# ADR-005: Meeting Intake Pipeline for Nuclear Facility Operations

**Status:** Proposed  
**Date:** 2026-01-14  
**Decision Makers:** Ben, Team

## Context

Nuclear facility teams—whether research systems, commercial plants, or national lab projects—discuss operational requirements, safety concerns, and action items in meetings. Currently:
- Meeting recordings exist in various platforms (Teams, Zoom, local)
- Requirements and action items are manually extracted (or lost)
- No systematic way to track requirements back to source discussions
- Issue tracker entries created ad-hoc, often duplicating existing items
- For regulatory purposes, traceability from decision to source is valuable

We need a system that:
1. Automatically processes meeting recordings
2. Extracts requirements, action items, and safety-relevant decisions with source attribution
3. Matches to existing issues (prefer append over create)
4. Respects access control (only attendees can see content)
5. Builds searchable context for future queries
6. Works across facility types and organizational boundaries

## Decision

We will build an agentic meeting intake pipeline using:
- **LangGraph** - Stateful agent workflows with human-in-the-loop
- **LLM (configurable)** - Claude, GPT-4, or local models for extraction
- **Speech-to-text (configurable)** - Whisper (local) or cloud services
- **PostgreSQL + pgvector** - Vector storage with row-level security
- **Issue tracker API** - GitLab, GitHub, Jira, or other (pluggable)

## Workflow

```
Recording → Transcribe → Extract → Match Issues → Human Review → Apply to Tracker
(any source)  (configurable)  (LLM)   (embeddings)   (required)    (pluggable)
                                              ↓
                                    Prefer APPEND over CREATE
```

## Key Design Decisions

### 1. LLM Provider: Configurable
- Support multiple providers: Claude, GPT-4, Gemini, local (Llama, Mistral)
- Facilities may have security requirements favoring local models
- Commercial plants may require air-gapped deployment
- Default: Claude (excellent structured extraction) or local Llama for offline

### 2. Human-in-the-Loop Review
- All extracted requirements queue for human approval
- System suggests: APPEND to existing issue or CREATE new
- Human can approve, edit, skip, or redirect
- Fast batch review UI (CLI or web)

### 3. Access Control via pgvector RLS
- Meeting attendees stored with each meeting
- PostgreSQL Row-Level Security restricts queries
- Users can only retrieve context from meetings they attended
- External attendees excluded initially (future enhancement)

### 4. Prefer Append Over Create
- Vector similarity matches requirements to existing issues
- Threshold (0.7) determines auto-suggest append
- Reduces issue sprawl, keeps related items together

### 5. Meeting RAG Knowledge Base

All meeting transcripts feed a shared RAG (Retrieval-Augmented Generation) system:

**Chunking strategy:**
- Split transcripts by speaker turn + time window (~2-3 min chunks)
- Preserve speaker attribution and timestamp in metadata
- Overlap chunks by 1 turn for context continuity

**Access control tiers:**
| Tier | Who Can Query | Use Case |
|------|---------------|----------|
| `attendee` | Only meeting attendees | Default for all meetings |
| `facility` | Anyone at same facility | Shared ops knowledge |
| `public` | All Axiom users | Cross-facility benchmarking |

**How it works:**
1. **Intake:** Transcript chunked and embedded after human review
2. **Storage:** Chunks stored in pgvector with `facility_id` and `meeting_id`
3. **Access:** PostgreSQL RLS enforces tier-based access at query time
4. **Query:** User asks "What did we decide about X?" → vector search → LLM synthesizes answer with source citations

**Cross-facility sharing:**
- Default: `attendee` only (private)
- Facility admin can promote to `facility` (shared within org)
- Explicit opt-in to `public` (requires review for sensitive content)
- Federated queries across facilities only see `public` chunks

## Alternatives Considered

| Component | Selected | Alternative | Reason |
|-----------|----------|-------------|--------|
| Agent framework | LangGraph | LangChain agents | LangGraph better state management, checkpoints |
| LLM | Configurable | Single provider | Facilities have different security/connectivity requirements |
| Transcription | Configurable | Single provider | Air-gapped sites need local; cloud sites can use services |
| Vector store | pgvector | ChromaDB | Single DB, RLS built-in |
| Issue tracker | Pluggable | GitLab only | Different orgs use GitLab, GitHub, Jira, etc. |

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                         MEETING INTAKE + RAG ARCHITECTURE                                │
├─────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                         │
│   INTAKE PIPELINE (LangGraph)                                                           │
│   ┌─────────┐   ┌───────────┐   ┌─────────┐   ┌─────────────┐   ┌─────────────────┐    │
│   │  Fetch  │──►│Transcribe │──►│ Extract │──►│Human Review │──►│ Apply to Tracker│    │
│   │ (plug.) │   │ (config.) │   │  (LLM)  │   │(checkpoint) │   │   (pluggable)   │    │
│   └─────────┘   └─────┬─────┘   └─────────┘   └─────────────┘   └─────────────────┘    │
│                       │                                                                 │
│                       ▼                                                                 │
│   RAG KNOWLEDGE BASE (pgvector + RLS)                                                   │
│   ┌─────────────────────────────────────────────────────────────────────────────────┐  │
│   │  meeting_chunks              meeting_access                                      │  │
│   │  ├─ chunk_id                 ├─ meeting_id                                       │  │
│   │  ├─ meeting_id (FK)          ├─ user_id                                          │  │
│   │  ├─ content                  └─ access_level (attendee|facility|public)          │  │
│   │  ├─ embedding (vector)                                                           │  │
│   │  ├─ speaker                  RLS: Users only see meetings they can access        │  │
│   │  └─ facility_id                                                                  │  │
│   └─────────────────────────────────────────────────────────────────────────────────┘  │
│                       │                                                                 │
│                       ▼                                                                 │
│   QUERY FLOW                                                                            │
│   ┌─────────────┐   ┌─────────────┐   ┌───────────────┐   ┌─────────────────────┐      │
│   │ User Query  │──►│ Embed Query │──►│ Vector Search │──►│ LLM + Context       │      │
│   │ "What did   │   │             │   │ (RLS filter)  │   │ Answer with source  │      │
│   │  we decide  │   │             │   │ Only returns  │   │ citations           │      │
│   │  about X?"  │   │             │   │ accessible    │   │                     │      │
│   └─────────────┘   └─────────────┘   │ meetings      │   └─────────────────────┘      │
│                                       └───────────────┘                                 │
│                                                                                         │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

## Consequences

### Positive
- Systematic capture of meeting requirements
- Traceability from issue to source discussion
- Reduced manual transcription work
- Searchable meeting context for future analysis
- Privacy-respecting access control

### Negative
- Recording platform API integration complexity (varies by platform)
- Transcription quality varies with audio quality
- LLM extraction may miss nuance
- Human review still required (not fully automated)

### Mitigations
- Fallback to manual transcript upload
- Human review catches extraction errors
- Iterative prompt improvement based on feedback
- Batch review UI for efficiency

## Multi-Facility Applicability

This tool is designed for any operations facility operations team:

| Facility Type | Example Use Cases |
|---------------|-------------------|
| **University systems** | Operations meetings, experiment planning, safety committee |
| **Commercial plants** | Outage planning, corrective action tracking, regulatory body prep |
| **National labs** | Project coordination, safety reviews, experiment design |
| **Regulatory bodies** | Inspection prep, finding tracking, public meeting records |

**Deployment options:**
- Cloud-hosted (for university/lab environments)
- On-premises (for commercial plants with security requirements)
- Air-gapped (for sensitive facilities, using local LLM)

## References

- [LangGraph](https://python.langchain.com/docs/langgraph)
- [pgvector](https://github.com/pgvector/pgvector)
- [OpenAI Whisper](https://openai.com/research/whisper) (local transcription)
- [Ollama](https://ollama.ai/) (local LLM hosting)
- [LiteLLM](https://github.com/BerriAI/litellm) (unified LLM API)
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
