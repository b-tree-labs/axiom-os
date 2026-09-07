# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Absorb adapters + the D2 import primitive (ADR-087 D2/D8, PRD F3)."""

from .base import AbsorbAdapter, AbsorbScan, FragmentCandidate, SkippedSource
from .extract import extract_by_source
from .importer import ImportReport, import_candidates

__all__ = [
    "AbsorbAdapter",
    "AbsorbScan",
    "FragmentCandidate",
    "ImportReport",
    "SkippedSource",
    "extract_by_source",
    "import_candidates",
]
