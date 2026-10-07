# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SimStudent profiles for the Prague simulator.

12 students with realistic variation across background, language,
engagement level, and confusion patterns. Used by the harness to
generate plausible agendas + ground-truth-aware rubric scoring.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SimStudent:
    """A synthetic student with a realistic profile."""

    student_id: str
    name: str
    background: str                # "physics" | "chemical_eng" | "general"
    language: str                  # ISO 639-1: "en" | "cs" | "de" | etc.
    engagement_level: str          # "low" | "medium" | "high"
    timezone_offset_hours: int     # relative to UTC
    confusion_patterns: tuple[str, ...] = ()
    pedagogy_preference: str = "socratic"  # "socratic" | "didactic" | "direct"

    def sessions_per_day_mean(self) -> float:
        """Poisson rate parameter for session starts."""
        return {"low": 0.2, "medium": 0.8, "high": 2.0}[self.engagement_level]


def classroom_of_12() -> list[SimStudent]:
    """The canonical Prague cohort — 12 diverse students.

    Distribution mimics Ben's Prague summer-course assumptions:
    mixed nationalities, mixed English fluency, mixed backgrounds,
    realistic engagement variance.
    """
    return [
        SimStudent("s01", "Alice", background="physics",
                   language="en", engagement_level="high",
                   timezone_offset_hours=-6,
                   confusion_patterns=("mass-energy equivalence",),
                   pedagogy_preference="socratic"),
        SimStudent("s02", "Bohdan", background="chemical_eng",
                   language="cs", engagement_level="medium",
                   timezone_offset_hours=1,
                   confusion_patterns=("decay chain direction",),
                   pedagogy_preference="didactic"),
        SimStudent("s03", "Chen", background="physics",
                   language="en", engagement_level="medium",
                   timezone_offset_hours=8,
                   pedagogy_preference="direct"),
        SimStudent("s04", "Dmitri", background="general",
                   language="en", engagement_level="low",
                   timezone_offset_hours=3,
                   confusion_patterns=("cross-section intuition",)),
        SimStudent("s05", "Elena", background="chemical_eng",
                   language="de", engagement_level="high",
                   timezone_offset_hours=1,
                   pedagogy_preference="socratic"),
        SimStudent("s06", "Farouk", background="physics",
                   language="en", engagement_level="medium",
                   timezone_offset_hours=3,
                   confusion_patterns=("fission vs fusion products",)),
        SimStudent("s07", "Giulia", background="chemical_eng",
                   language="en", engagement_level="high",
                   timezone_offset_hours=1,
                   pedagogy_preference="direct"),
        SimStudent("s08", "Hiro", background="general",
                   language="en", engagement_level="low",
                   timezone_offset_hours=9,
                   confusion_patterns=("moderator vs reflector roles",)),
        SimStudent("s09", "Ingrid", background="physics",
                   language="en", engagement_level="medium",
                   timezone_offset_hours=1,
                   pedagogy_preference="didactic"),
        SimStudent("s10", "Jean", background="general",
                   language="en", engagement_level="medium",
                   timezone_offset_hours=1,
                   confusion_patterns=("critical mass vs criticality",)),
        SimStudent("s11", "Kofi", background="physics",
                   language="en", engagement_level="high",
                   timezone_offset_hours=0,
                   pedagogy_preference="socratic"),
        SimStudent("s12", "Liu", background="chemical_eng",
                   language="en", engagement_level="low",
                   timezone_offset_hours=8,
                   confusion_patterns=("neutron energy spectrum",)),
    ]
