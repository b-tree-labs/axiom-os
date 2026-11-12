# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Signal detection and intelligence extraction.

Houses **SCAN**, the Read agent in the platform's REPL cycle. SCAN watches
sources (inbox, OneDrive, Teams, GitHub/GitLab, calendar, federation peer
events) and emits structured signals into the local stream for downstream
agents — primarily AXI for routing, PRESS for documents, TIDY for
retention. See ``agents/scan/persona.md`` for SCAN's role definition.

The package also implements the sense-→-synthesis-→-publish loop:
extracts design signals, clusters by PRD, synthesizes updates, and
tracks loop health metrics to increase velocity over time.
"""

from .clustering import PRDClusterer, SignalCluster
from .extractors import CalendarExtractor, FeedbackExtractor, NotesExtractor
from .loop import (
    ArtifactType,
    FeedbackType,
    LoopHealthMetrics,
    LoopIteration,
    LoopStage,
    LoopTracker,
    SubscriberRole,
    Subscription,
)
from .models import Changelog, ChangelogEntry, Extraction, Signal, SignalManifest
from .synthesis import BriefingGenerator, DesignBriefing, PRDUpdateDraft, PRDUpdater

__version__ = "0.1.0"

__all__ = [
    # Models
    "Signal",
    "Extraction",
    "Changelog",
    "ChangelogEntry",
    "SignalManifest",
    # Extractors
    "CalendarExtractor",
    "NotesExtractor",
    "FeedbackExtractor",
    # Clustering
    "PRDClusterer",
    "SignalCluster",
    # Synthesis
    "PRDUpdater",
    "PRDUpdateDraft",
    "BriefingGenerator",
    "DesignBriefing",
    # Loop Tracking
    "LoopStage",
    "FeedbackType",
    "SubscriberRole",
    "ArtifactType",
    "Subscription",
    "LoopIteration",
    "LoopHealthMetrics",
    "LoopTracker",
]
