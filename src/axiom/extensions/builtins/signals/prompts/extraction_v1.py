# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Signal extraction prompts v1.

These prompts are used by extractors to identify design signals
in raw source content.
"""

CALENDAR_EXTRACTION_PROMPT = """
You are extracting design signals from a calendar event.

## Calendar Event
Title: {title}
Date: {date}
Duration: {duration}
Attendees: {attendees}
Description:
{description}

## Target PRDs
The user is working on these PRDs:
{prd_list}

## Your Task
Extract design-relevant signals from this event. For each signal, identify:
1. **signal_type**: One of: requirement, decision, question, insight, action_item
2. **content**: The actual information (2-3 sentences max)
3. **prd_target**: Which PRD this relates to, or null if unclear
4. **people**: Who contributed to or owns this signal
5. **confidence**: 0.0-1.0 how confident you are in this extraction

## Output Format
Return a JSON array of signals:
```json
[
  {{
    "signal_type": "decision",
    "content": "Team decided to use PostgreSQL for persistence layer",
    "prd_target": "ops_log",
    "people": ["Alice", "Bob"],
    "confidence": 0.9
  }}
]
```

## Guidelines
- Only extract signals that have clear design relevance
- Prefer specificity over vagueness
- If the event is purely social/admin with no design content, return []
- Meeting titles like "Design Review" or "Architecture Discussion" likely contain signals
- Action items assigned to specific people are high-value signals

Extract the signals:
"""

NOTES_EXTRACTION_PROMPT = """
You are extracting design signals from meeting notes or documentation.

## Source Document
Filename: {filename}
Date: {date}
Content:
{content}

## Target PRDs
The user is working on these PRDs:
{prd_list}

## Your Task
Extract design-relevant signals from this document. Focus on:
- Decisions made (look for "decided", "agreed", "will use")
- Requirements captured (look for "must", "should", "need to")
- Open questions (look for "?", "TBD", "need to figure out")
- Insights (look for learnings, discoveries, "realized that")
- Action items (look for names with tasks, "TODO", "Action:")

## Output Format
Return a JSON array of signals:
```json
[
  {{
    "signal_type": "requirement",
    "content": "System must support 100 concurrent users",
    "prd_target": "operator_dashboard",
    "people": ["Product Owner"],
    "confidence": 0.85
  }}
]
```

## Guidelines
- Extract each distinct signal separately (don't combine)
- Include context but keep content concise (2-3 sentences)
- Attribute signals to people when mentioned
- If a note has YAML frontmatter with tags, use those for PRD targeting
- Code snippets or technical specs may contain implicit requirements

Extract the signals:
"""

SIGNAL_CLASSIFICATION_PROMPT = """
You are classifying a design signal into categories.

## Signal
Content: {content}
Source: {source}
Date: {date}

## Classification Task
Determine the most appropriate:

1. **signal_type** (choose one):
   - `requirement`: A stated need or constraint
   - `decision`: A choice that was made
   - `question`: An open question or uncertainty
   - `insight`: A learning, discovery, or realization
   - `action_item`: A task assigned to someone

2. **prd_target** (choose one or null):
   - `ops_log`: Operations logging and audit trail
   - `experiment_manager`: Experiment lifecycle management
   - `operator_dashboard`: Real-time operator interface
   - `researcher_dashboard`: Analysis and research tools
   - `null`: If unclear or spans multiple PRDs

3. **urgency** (choose one):
   - `high`: Blocking or time-sensitive
   - `medium`: Important but not blocking
   - `low`: Nice to know, not urgent

4. **quality_score** (0.0-1.0):
   - How actionable and specific is this signal?
   - 0.9+: Very specific, immediately actionable
   - 0.7-0.9: Clear but may need clarification
   - 0.5-0.7: Somewhat vague, useful context
   - <0.5: Very vague, limited value

## Output Format
```json
{{
  "signal_type": "decision",
  "prd_target": "ops_log",
  "urgency": "medium",
  "quality_score": 0.85,
  "reasoning": "One sentence explaining the classification"
}}
```

Classify:
"""

# Keyword patterns for rule-based extraction fallback
SIGNAL_TYPE_KEYWORDS = {
    "requirement": [
        "must", "shall", "should", "need to", "required",
        "requirement", "constraint", "mandatory",
    ],
    "decision": [
        "decided", "agreed", "will use", "going with",
        "decision", "chose", "selected", "approved",
    ],
    "question": [
        "?", "tbd", "unclear", "need to figure out",
        "open question", "wondering", "not sure",
    ],
    "insight": [
        "realized", "learned", "discovered", "insight",
        "turns out", "interesting finding", "noticed",
    ],
    "action_item": [
        "todo", "action:", "will do", "assigned to",
        "follow up", "next step", "owner:",
    ],
}

PRD_TARGET_KEYWORDS = {
    "ops_log": [
        "log", "audit", "history", "record", "event log",
        "ops", "operation", "tracking",
    ],
    "experiment_manager": [
        "experiment", "run", "workflow", "batch",
        "execution", "parameter", "protocol",
    ],
    "operator_dashboard": [
        "operator", "dashboard", "monitor", "alert",
        "real-time", "status", "control panel",
    ],
    "researcher_dashboard": [
        "researcher", "analysis", "data", "visualization",
        "export", "report", "query",
    ],
}
