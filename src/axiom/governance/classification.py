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

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any


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


# ---------------------------------------------------------------------------
# The export-control label (MVP-22): one model, on every item, upload, session
# and export.
#
# Export control attaches to the controlled codes (see `controlled_code`), not
# to data; most items are therefore labelled `internal`. The label is how the
# decision about one item travels with it once it is made. It is a
# `Classification`, so a label and a person's clearance compare with the same
# predicate that routes notifications.
#
# A label is *proposed* by a detector (the library's EC hook, the ingest
# screen) and *confirmed* by a person. Only a confirmed label lets an item be
# retrieved or exported; a proposed one is a suggestion on a chip.
# ---------------------------------------------------------------------------

PROPOSED = "proposed"
CONFIRMED = "confirmed"

#: What a detector's verdict proposes. The library's EC hook says
#: clear / sensitive / held; the ingest screen says none / sensitive /
#: controlled. Both map here, so there is one vocabulary downstream.
_VERDICT_TO_LABEL: dict[str, Classification] = {
    "clear": Classification.INTERNAL,
    "none": Classification.INTERNAL,
    "sensitive": Classification.REGULATED,
    "held": Classification.CONTROLLED,
    "controlled": Classification.CONTROLLED,
}


class Unlabelled(PermissionError):
    """An act that needs a confirmed export-control label met an item without one."""

    def __init__(self, act: str, subject: str = "") -> None:
        self.act = act
        self.subject = subject
        what = f"{subject!r} " if subject else "this item "
        super().__init__(
            f"{what}has no confirmed export-control label, so it cannot be {act}. "
            "Confirm its label first."
        )


class AboveClearance(PermissionError):
    """The item's label is above the person's export-control clearance."""

    def __init__(self, label: Classification, clearance: Classification) -> None:
        self.label = label
        self.clearance = clearance
        super().__init__(
            f"this item is labelled {label.value}, above your clearance ({clearance.value})"
        )


@dataclass(frozen=True)
class ExportControlLabel:
    """An export-control label and whether a person has confirmed it."""

    classification: Classification
    state: str = PROPOSED
    by: str = ""
    at: str = ""
    basis: str = ""

    @property
    def confirmed(self) -> bool:
        return self.state == CONFIRMED

    def to_json(self) -> dict[str, Any]:
        return {
            "value": self.classification.value,
            "state": self.state,
            "by": self.by,
            "at": self.at,
            "basis": self.basis,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any] | None) -> ExportControlLabel | None:
        if not raw or not raw.get("value"):
            return None
        value = parse_label(str(raw["value"]))
        if value is None:
            return None
        state = CONFIRMED if raw.get("state") == CONFIRMED else PROPOSED
        return cls(
            classification=value,
            state=state,
            by=str(raw.get("by") or ""),
            at=str(raw.get("at") or ""),
            basis=str(raw.get("basis") or ""),
        )

    def confirm(self, by: str, *, at: datetime | None = None) -> ExportControlLabel:
        return confirmed_label(self.classification, by=by, at=at, basis=self.basis)


def parse_label(value: str | None) -> Classification | None:
    """A label value, or ``None`` when the string is not one (never raises)."""
    if not value:
        return None
    try:
        return Classification.from_str(value)
    except ValueError:
        return None


def propose_label(verdict: str, *, basis: str = "") -> ExportControlLabel:
    """The label a detector's verdict proposes. Unknown verdicts propose the strictest."""
    value = _VERDICT_TO_LABEL.get((verdict or "").strip().lower(), Classification.CONTROLLED)
    return ExportControlLabel(classification=value, state=PROPOSED, basis=basis or f"detector: {verdict}")


def confirmed_label(
    value: Classification | str, *, by: str, at: datetime | None = None, basis: str = ""
) -> ExportControlLabel:
    """A label a person confirmed. ``by`` is required: a confirmation names who."""
    if not by:
        raise ValueError("a confirmed label names the person who confirmed it")
    cls_value = value if isinstance(value, Classification) else Classification.from_str(value)
    return ExportControlLabel(
        classification=cls_value,
        state=CONFIRMED,
        by=by,
        at=(at or datetime.now(UTC)).isoformat(),
        basis=basis or "confirmed by a person",
    )


def require_label(label: ExportControlLabel | None, *, act: str, subject: str = "") -> ExportControlLabel:
    """The confirmed label, or :class:`Unlabelled` naming the act refused."""
    if label is None or not label.confirmed:
        raise Unlabelled(act, subject)
    return label


# ---------------------------------------------------------------- clearance

#: What a verified person at a site reads when nothing says more. Regulated
#: and controlled items need a clearance attribute (the directory, ADR-103).
DEFAULT_CLEARANCE = Classification.INTERNAL
#: No principal: the fail-closed floor.
ANONYMOUS_CLEARANCE = Classification.PUBLIC

ClearanceResolver = Callable[[str], "Classification | None"]
_resolver: ClearanceResolver | None = None


def set_clearance_resolver(resolver: ClearanceResolver | None) -> None:
    """Bind where a person's clearance attribute comes from (the directory; tests)."""
    global _resolver
    _resolver = resolver


def clearance_for(principal: str | None) -> Classification:
    """A person's export-control clearance. Fails closed to the floor on any doubt."""
    if not principal:
        return ANONYMOUS_CLEARANCE
    if _resolver is not None:
        try:
            got = _resolver(principal)
        except Exception:  # noqa: BLE001 - a resolver fault is no elevation
            return DEFAULT_CLEARANCE
        if isinstance(got, Classification):
            return got
        parsed = parse_label(got) if isinstance(got, str) else None
        if parsed is not None:
            return parsed
    return DEFAULT_CLEARANCE


def permits(clearance: Classification, label: ExportControlLabel | Classification) -> bool:
    """Whether a person with ``clearance`` may receive an item with ``label``."""
    value = label.classification if isinstance(label, ExportControlLabel) else label
    return classification_lte(value, clearance)


__all__ = [
    "ANONYMOUS_CLEARANCE",
    "CONFIRMED",
    "DEFAULT_CLEARANCE",
    "PROPOSED",
    "AboveClearance",
    "Classification",
    "ExportControlLabel",
    "Unlabelled",
    "classification_lte",
    "clearance_for",
    "confirmed_label",
    "parse_label",
    "permits",
    "propose_label",
    "require_label",
    "set_clearance_resolver",
]
