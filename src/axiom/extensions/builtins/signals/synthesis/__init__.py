# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Synthesis module - transforms signal clusters into actionable outputs."""

from .briefing_generator import BriefingGenerator, DesignBriefing
from .prd_updater import PRDUpdateDraft, PRDUpdater

__all__ = [
    "PRDUpdater",
    "PRDUpdateDraft",
    "BriefingGenerator",
    "DesignBriefing",
]
