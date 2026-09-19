# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Prompt templates for signal extraction and synthesis."""

from .briefing_v1 import (
    AUDIENCE_ADAPTATION_PROMPT,
    BRIEFING_SECTION_PROMPT,
    BRIEFING_SUMMARY_PROMPT,
)
from .extraction_v1 import (
    CALENDAR_EXTRACTION_PROMPT,
    NOTES_EXTRACTION_PROMPT,
    SIGNAL_CLASSIFICATION_PROMPT,
)
from .prd_synthesis_v1 import (
    PRD_DECISIONS_PROMPT,
    PRD_QUESTIONS_PROMPT,
    PRD_REQUIREMENTS_PROMPT,
    PRD_SECTION_SYNTHESIS_PROMPT,
)

__all__ = [
    # Extraction
    "CALENDAR_EXTRACTION_PROMPT",
    "NOTES_EXTRACTION_PROMPT",
    "SIGNAL_CLASSIFICATION_PROMPT",
    # PRD Synthesis
    "PRD_SECTION_SYNTHESIS_PROMPT",
    "PRD_REQUIREMENTS_PROMPT",
    "PRD_DECISIONS_PROMPT",
    "PRD_QUESTIONS_PROMPT",
    # Briefing
    "BRIEFING_SUMMARY_PROMPT",
    "BRIEFING_SECTION_PROMPT",
    "AUDIENCE_ADAPTATION_PROMPT",
]
