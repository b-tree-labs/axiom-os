# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Axiom ArtifactRegistry.

Content-addressed registry for every named, versioned, attributable thing
in Axiom: courses, classrooms, evals, questionnaires, findings, models,
research bundles. Every artifact has a content hash; optionally a
signature from a context's private key (ADR-020/021 identity chain).

Slice: Classroom Phase 1 foundation.
"""

from __future__ import annotations

from axiom.artifacts.registry import Artifact, ArtifactRegistry

__all__ = ["Artifact", "ArtifactRegistry"]
