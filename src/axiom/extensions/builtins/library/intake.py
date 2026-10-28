# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One upload, from bytes to a filed document.

Classification decides what a file is; the taxonomy decides where that kind
files; intake is the single call that does both and reports the whole decision.

Reporting the whole decision is the point. A filed document that carries only
its category tells a user where it went and nothing about why, so a wrong
placement becomes an argument. Carrying the type, the confidence, the classifier
that decided and the attributes it extracted turns the same disagreement into a
correction someone can make and a rule someone can fix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from axiom.extensions.builtins.library.classify import Classification, ClassifierChain
from axiom.extensions.builtins.library.taxonomy import LibraryTaxonomy, get_taxonomy


@dataclass(frozen=True)
class IntakeResult:
    """What an upload was decided to be, and where it filed."""

    file_name: str
    document_type: str
    category: str
    label: str
    confidence: float
    classifier: str
    taxonomy: str
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def was_recognised(self) -> bool:
        """Whether anything identified this, or it fell through to the fallback.

        Distinct from confidence: a chain can be confident and still land on the
        fallback type, and a user deserves to know which happened.
        """
        return self.classifier not in ("", "fallback")

    def describe(self) -> str:
        if not self.was_recognised:
            return f"{self.file_name}: unrecognised, filed under {self.category}"
        return (
            f"{self.file_name}: {self.label} ({self.confidence:.0%} confident, "
            f"{self.classifier}) filed under {self.category}"
        )


def intake(
    file_name: str,
    *,
    chain: ClassifierChain,
    taxonomy: str | LibraryTaxonomy,
    mime_type: str | None = None,
    content: bytes | None = None,
) -> IntakeResult:
    """Classify one upload and place it, in a single call."""
    resolved = (
        taxonomy if isinstance(taxonomy, LibraryTaxonomy) else get_taxonomy(taxonomy)
    )
    classification: Classification = chain.classify(file_name, mime_type, content)

    return IntakeResult(
        file_name=file_name,
        document_type=classification.document_type,
        category=resolved.category_for(classification.document_type),
        label=resolved.label_for(classification.document_type),
        confidence=classification.confidence,
        classifier=classification.classifier,
        taxonomy=resolved.name,
        attributes=dict(classification.attributes),
    )


__all__ = ["IntakeResult", "intake"]
