# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Axiom Classroom module.

Phase 1: Course + Classroom artifacts, enrollment, archive lifecycle.
Phase 2: periods, presence, @agent addressing, NL policy broadcasting.
Phase 3 (v3): alumni persistent identity, longitudinal research.

All classroom artifacts live in the shared ArtifactRegistry. CourseService
and ClassroomService are thin domain wrappers that enforce lifecycle rules.
"""

from __future__ import annotations

from axiom.classroom.classroom import Classroom, ClassroomService
from axiom.classroom.course import Course, CourseService
from axiom.classroom.period import Period, PeriodService

__all__ = [
    "Classroom",
    "ClassroomService",
    "Course",
    "CourseService",
    "Period",
    "PeriodService",
]
