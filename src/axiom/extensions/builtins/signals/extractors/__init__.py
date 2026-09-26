# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Signal extractors for axi signal.

Extracts signals from various sources to feed the design loop:
- calendar: Meeting events, deadlines
- notes: Personal notes, meeting minutes
- voice: Voice memos, transcriptions
- feedback: User feedback (surveys, support, analytics)
- docflow_review: External document changes (MS 365 Word, Google Docs)
"""

from .calendar import CalendarExtractor
from .docflow_review import DocFlowReviewExtractor, DocFormat, register_prd
from .feedback import FeedbackExtractor
from .notes import NotesExtractor

__all__ = [
    "CalendarExtractor",
    "NotesExtractor",
    "FeedbackExtractor",
    "DocFlowReviewExtractor",
    "DocFormat",
    "register_prd",
]
