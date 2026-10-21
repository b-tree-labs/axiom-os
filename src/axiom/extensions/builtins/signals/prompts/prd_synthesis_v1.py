# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PRD synthesis prompts v1.

These prompts generate PRD section updates from clustered signals.
"""

PRD_SECTION_SYNTHESIS_PROMPT = """
You are synthesizing signals into a PRD section update.

## Target PRD
Name: {prd_name}
Section: {section_name}

## Current Section Content
{current_content}

## Signals to Incorporate
{signals_json}

## Your Task
Generate an updated section that:
1. Preserves important existing content
2. Incorporates new information from signals
3. Maintains consistent formatting
4. Includes inline citations like [sig-001]

## Output Format
Return JSON:
```json
{{
  "updated_content": "The full updated section in markdown",
  "changes_summary": "2-3 sentences summarizing what changed",
  "confidence": 0.85,
  "citations_used": ["sig-001", "sig-003"]
}}
```

## Guidelines
- Don't remove existing content unless signals explicitly contradict it
- Mark new additions with [NEW] prefix for review
- Keep the same heading level and formatting style
- If signals conflict with each other, note the conflict

Generate the update:
"""

PRD_REQUIREMENTS_PROMPT = """
You are synthesizing requirement signals into PRD requirements format.

## Target PRD: {prd_name}

## Current Requirements
{current_requirements}

## New Requirement Signals
{signals_json}

## Your Task
Generate properly formatted requirements:
- Use REQ-XXX numbering (continue from highest existing)
- Include priority (P0/P1/P2)
- Include source citation [sig-XXX]
- Mark as [NEW] for review

## Output Format
```markdown
### Requirements

#### Functional Requirements

- **REQ-001** [P1]: System must support user authentication [sig-001] [NEW]
- **REQ-002** [P0]: Data must be persisted within 5 seconds [sig-003] [NEW]

#### Non-Functional Requirements

- **REQ-010** [P1]: System must handle 100 concurrent users [sig-005] [NEW]
```

## Guidelines
- P0 = Must have for MVP
- P1 = Important, needed for launch
- P2 = Nice to have, can defer
- Deduplicate similar requirements
- Flag conflicts between new and existing requirements

Generate the requirements section:
"""

PRD_DECISIONS_PROMPT = """
You are synthesizing decision signals into PRD design decisions format.

## Target PRD: {prd_name}

## Current Design Decisions
{current_decisions}

## New Decision Signals
{signals_json}

## Your Task
Generate properly formatted design decisions:
- Use ADR (Architecture Decision Record) style
- Include context, decision, and consequences
- Include source citation [sig-XXX]
- Mark as [NEW] for review

## Output Format
```markdown
### Design Decisions

#### DD-001: Database Selection [NEW]

**Context:** Need to choose a database for the ops log storage.

**Decision:** Use PostgreSQL with TimescaleDB extension. [sig-001]

**Consequences:**
- (+) Strong time-series support
- (+) Team familiarity
- (-) Requires additional extension management

**Status:** Proposed | Accepted | Deprecated
```

## Guidelines
- Number decisions sequentially (DD-XXX)
- Include rationale when available from signals
- Note trade-offs in consequences
- If decision reverses previous decision, note it

Generate the decisions section:
"""

PRD_QUESTIONS_PROMPT = """
You are synthesizing question signals into PRD open questions format.

## Target PRD: {prd_name}

## Current Open Questions
{current_questions}

## New Question Signals
{signals_json}

## Your Task
Generate properly formatted open questions:
- Group by theme/area
- Include owner/assignee if known
- Include due date if mentioned
- Mark as [NEW] for review
- Check if any existing questions are now answered

## Output Format
```markdown
### Open Questions

#### Architecture
- [ ] How will we handle data replication? (Owner: @alice, Due: 2024-02-01) [sig-001] [NEW]
- [x] What database should we use? → Resolved: PostgreSQL [DD-001]

#### UX
- [ ] Should operators see all experiments or only their own? [sig-003] [NEW]
```

## Guidelines
- Use checkbox format: [ ] open, [x] resolved
- Include owner when mentioned in signals
- Link resolved questions to related decisions
- Flag conflicting questions

Generate the questions section:
"""

# Section-specific formatting templates
SECTION_TEMPLATES = {
    "requirements": {
        "heading": "## Requirements",
        "subheadings": ["### Functional Requirements", "### Non-Functional Requirements"],
        "item_format": "- **REQ-{num:03d}** [{priority}]: {content} [{citation}]",
    },
    "design_decisions": {
        "heading": "## Design Decisions",
        "item_format": "### DD-{num:03d}: {title}\n\n**Context:** {context}\n\n**Decision:** {decision} [{citation}]\n\n**Status:** {status}",
    },
    "open_questions": {
        "heading": "## Open Questions",
        "item_format": "- [ ] {content} (Owner: {owner}) [{citation}]",
    },
    "overview": {
        "heading": "## Overview",
        "item_format": "{content}",
    },
    "milestones": {
        "heading": "## Milestones",
        "item_format": "- **{date}**: {content} [{citation}]",
    },
}

# Confidence thresholds for auto-approval
CONFIDENCE_THRESHOLDS = {
    "auto_approve": 0.9,  # Above this, can auto-apply
    "needs_review": 0.7,  # Between this and auto_approve, needs human review
    "reject": 0.5,  # Below this, don't generate draft
}
