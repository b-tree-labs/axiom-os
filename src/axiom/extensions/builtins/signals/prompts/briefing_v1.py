# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Briefing synthesis prompts v1.

These prompts generate narrative briefings from clustered signals.
"""

BRIEFING_SUMMARY_PROMPT = """
You are generating an executive summary for a design briefing.

## Briefing Context
Type: {briefing_type}
Audience: {audience}
Time Period: {start_date} to {end_date}
PRDs Covered: {prd_list}

## Signal Summary
Total signals: {signal_count}
By type:
{type_breakdown}

## Key Signals (highest quality)
{top_signals_json}

## Your Task
Write a concise executive summary that:
1. Highlights the most important developments
2. Matches the audience's information needs
3. Uses appropriate tone and detail level
4. Stays within {word_limit} words

## Audience Guidelines
- **Executive**: Focus on outcomes, risks, timeline impacts
- **Manager**: Focus on progress, blockers, resource needs
- **Team Lead**: Focus on decisions, technical direction, action items
- **Engineer**: Focus on technical details, implementation decisions
- **External**: Focus on high-level progress, no internal details

## Output Format
Return the summary as plain text, no JSON wrapping.

Generate the summary:
"""

BRIEFING_SECTION_PROMPT = """
You are generating a section for a design briefing.

## Section
Heading: {section_heading}
Purpose: {section_purpose}
Word Limit: {word_limit}

## Signals for This Section
{signals_json}

## Audience
{audience} - {audience_description}

## Your Task
Generate content for this section that:
1. Synthesizes the signals into coherent narrative
2. Uses bullet points for clarity
3. Includes inline citations [sig-XXX]
4. Stays within word limit

## Output Format
```json
{{
  "content": "The section content in markdown",
  "citations": ["sig-001", "sig-003"]
}}
```

Generate the section:
"""

AUDIENCE_ADAPTATION_PROMPT = """
You are adapting a briefing for a specific audience.

## Original Content
{original_content}

## Target Audience
{target_audience}

## Adaptation Rules
{adaptation_rules}

## Your Task
Rewrite the content for the target audience:
1. Adjust detail level (more/less technical)
2. Adjust tone (formal/informal)
3. Focus on what this audience cares about
4. Keep citations intact

## Output
Return the adapted content as plain text.

Adapt:
"""

# Audience profiles for content adaptation
AUDIENCE_PROFILES = {
    "executive": {
        "description": "C-level or VP, limited time, cares about outcomes",
        "detail_level": "high-level",
        "tone": "formal, concise",
        "focus": ["outcomes", "risks", "timeline", "resources"],
        "avoid": ["implementation details", "technical jargon", "process minutiae"],
        "word_multiplier": 0.5,  # 50% of base word count
    },
    "manager": {
        "description": "Project/product manager, needs to track progress",
        "detail_level": "medium",
        "tone": "professional, action-oriented",
        "focus": ["progress", "blockers", "decisions", "dependencies"],
        "avoid": ["deep technical details", "code-level decisions"],
        "word_multiplier": 0.75,
    },
    "team_lead": {
        "description": "Technical lead, needs to guide team",
        "detail_level": "detailed",
        "tone": "technical but clear",
        "focus": ["technical decisions", "architecture", "trade-offs", "action items"],
        "avoid": ["business strategy", "high-level metrics"],
        "word_multiplier": 1.0,
    },
    "engineer": {
        "description": "Individual contributor, needs implementation details",
        "detail_level": "very detailed",
        "tone": "technical, specific",
        "focus": ["implementation details", "APIs", "data models", "constraints"],
        "avoid": ["management concerns", "resource allocation"],
        "word_multiplier": 1.25,
    },
    "external": {
        "description": "Stakeholder outside the team",
        "detail_level": "high-level",
        "tone": "formal, polished",
        "focus": ["progress", "outcomes", "timeline"],
        "avoid": ["internal discussions", "technical debt", "team dynamics"],
        "word_multiplier": 0.6,
    },
}

# Section purposes for different briefing types
SECTION_PURPOSES = {
    "daily_standup": {
        "Yesterday's Progress": "What was accomplished since last standup",
        "Today's Focus": "What will be worked on today",
        "Blockers": "What is preventing progress",
    },
    "weekly_summary": {
        "Key Accomplishments": "Major achievements this week",
        "Decisions Made": "Important decisions and their rationale",
        "Open Questions": "Unresolved questions needing attention",
        "Next Week Focus": "Priorities for the coming week",
    },
    "stakeholder_update": {
        "Progress Highlights": "Notable progress toward goals",
        "Milestone Status": "Where we are against plan",
        "Risk & Mitigation": "Active risks and how we're addressing them",
        "Upcoming Milestones": "What to expect next",
    },
    "decision_log": {
        "Decisions This Period": "Decisions made during this period",
        "Rationale Summary": "Why these decisions were made",
        "Impact Assessment": "Expected effects of decisions",
        "Pending Decisions": "Decisions still needed",
    },
    "progress_narrative": {
        "Where We Started": "Initial state at period start",
        "What Changed": "Key changes and events",
        "Where We Are Now": "Current state",
        "What's Next": "Upcoming work and timeline",
    },
}

# Word limits by briefing type
BRIEFING_WORD_LIMITS = {
    "daily_standup": 200,
    "weekly_summary": 500,
    "stakeholder_update": 400,
    "decision_log": 600,
    "risk_report": 400,
    "progress_narrative": 800,
}
