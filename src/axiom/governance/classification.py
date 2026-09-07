# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classification — the data-tier label every `ActionEnvelope` carries.

Per spec-governance-fabric §1.2: classification is **of the resource being
acted on**, not of the actor. Every primitive that does classification
routing compares envelope classification against channel/resource ceilings.

Distinct from `axiom.vega.federation.policy.VisibilityHorizon` (which is
about cross-cohort *outflow*); classification is the data-tier label, the
horizon is about who-may-see across the federation.
"""

from __future__ import annotations

from enum import Enum


class Classification(str, Enum):
    """Data-tier label. Ordered lowest (most permissive) → highest (most restrictive).

    The four tiers map to `spec-classification-boundary.md`:

    - ``PUBLIC`` — no restriction; e.g. public docs, open-source code.
    - ``INTERNAL`` — institutional-internal; default for most platform receipts.
    - ``REGULATED`` — CUI / EAR-restricted; access subject to formal authority.
    - ``CONTROLLED`` — ITAR / Part 810 / NSI; access subject to clearance + record.

    The string values are stable across releases (JSON / TOML serialize
    plainly without custom encoders).
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    REGULATED = "regulated"
    CONTROLLED = "controlled"

    @property
    def tier(self) -> int:
        """Numeric tier for ``<=``-style ceiling comparisons.

        Higher = more restrictive. ``PUBLIC`` = 0; ``CONTROLLED`` = 3.
        """
        return _TIERS[self]

    @classmethod
    def from_str(cls, value: str) -> Classification:
        """Parse a string into a `Classification`, case-insensitive.

        Raises ``ValueError`` with the candidate list when input isn't a
        known tier name.
        """
        normalized = value.strip().lower()
        for member in cls:
            if member.value == normalized:
                return member
        candidates = ", ".join(m.value for m in cls)
        raise ValueError(
            f"unknown classification {value!r}; candidates: {candidates}"
        )


_TIERS: dict[Classification, int] = {
    Classification.PUBLIC: 0,
    Classification.INTERNAL: 1,
    Classification.REGULATED: 2,
    Classification.CONTROLLED: 3,
}


def classification_lte(
    actor: Classification, ceiling: Classification
) -> bool:
    """Return True when ``actor`` is at-or-below ``ceiling``.

    The load-bearing predicate for classification routing: a notification
    classified ``REGULATED`` cannot route through a channel whose ceiling
    is ``INTERNAL``.
    """
    return actor.tier <= ceiling.tier


__all__ = ["Classification", "classification_lte"]
