# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Decide what an uploaded file is, cheaply first and confidently last.

Classification is a chain, ordered cheapest-first: a filename rule costs
nothing, a content sniff costs a read, a model costs a call. The first
classifier confident enough wins, so most uploads never reach the expensive end.

Two properties are worth more than accuracy here.

*It always answers.* An upload that cannot be classified files under the
taxonomy's fallback rather than being rejected. A library that refuses what it
does not recognise loses documents, and the document a user could not file is
the one they needed.

*It says how sure it was, and who said so.* A placement a user disagrees with is
a conversation about a confidence score and a named classifier, not an argument
with an oracle. Carrying that also makes the chain tunable from evidence rather
than from opinion.

Domain-extracted detail rides in ``attributes`` rather than in typed fields. One
product pulls nutrients and a field name out of a soil report; another pulls an
isotope and an irradiation id. The core must not grow a column per product, so
it carries a mapping it never interprets.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

#: Below this, a classifier's answer is treated as a guess rather than a result.
#: Chosen so a weak filename match does not outrank a later content read.
DEFAULT_MIN_CONFIDENCE = 0.35


@dataclass(frozen=True)
class Classification:
    """What a file is, how sure we are, and who decided."""

    document_type: str
    confidence: float = 0.0
    classifier: str = ""
    #: Domain-extracted detail the core never interprets.
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def is_confident(self) -> bool:
        return self.confidence >= DEFAULT_MIN_CONFIDENCE


@runtime_checkable
class Classifier(Protocol):
    """Anything that can look at a file and venture a type."""

    name: str

    def classify(
        self, file_name: str, mime_type: str | None, content: bytes | None
    ) -> Classification | None: ...


@dataclass
class ClassifierChain:
    """Ordered classifiers, cheapest first, with a taxonomy's fallback last."""

    classifiers: Sequence[Classifier]
    fallback_type: str = "other"
    min_confidence: float = DEFAULT_MIN_CONFIDENCE

    def classify(
        self,
        file_name: str,
        mime_type: str | None = None,
        content: bytes | None = None,
    ) -> Classification:
        """Run the chain and return the first confident answer, or the fallback.

        A classifier that raises is skipped and reported: one broken rule must
        not make every upload unclassifiable, and it must not do so silently
        either — a chain quietly down to its fallback looks exactly like a
        library full of files nobody could identify.
        """
        best: Classification | None = None

        for classifier in self.classifiers:
            name = getattr(classifier, "name", type(classifier).__name__)
            try:
                result = classifier.classify(file_name, mime_type, content)
            except Exception:  # noqa: BLE001 - one rule must not sink intake
                log.exception(
                    "classifier %r failed on %r; continuing down the chain", name, file_name
                )
                continue
            if result is None:
                continue
            stamped = (
                result if result.classifier else _with_classifier(result, name)
            )
            if stamped.confidence >= self.min_confidence:
                return stamped
            # Keep the strongest weak answer: better than the fallback if
            # nothing downstream does better.
            if best is None or stamped.confidence > best.confidence:
                best = stamped

        if best is not None:
            return best
        return Classification(
            document_type=self.fallback_type, confidence=0.0, classifier="fallback"
        )


def _with_classifier(result: Classification, name: str) -> Classification:
    return Classification(
        document_type=result.document_type,
        confidence=result.confidence,
        classifier=name,
        attributes=result.attributes,
    )


@dataclass
class FilenameClassifier:
    """The cheapest rule: a pattern over the name, no read at all."""

    name: str
    rules: Sequence[tuple[Callable[[str], bool], str, float]]

    def classify(
        self, file_name: str, mime_type: str | None, content: bytes | None
    ) -> Classification | None:
        lowered = (file_name or "").lower()
        for matches, document_type, confidence in self.rules:
            if matches(lowered):
                return Classification(
                    document_type=document_type,
                    confidence=confidence,
                    classifier=self.name,
                )
        return None


__all__ = [
    "DEFAULT_MIN_CONFIDENCE",
    "Classification",
    "Classifier",
    "ClassifierChain",
    "FilenameClassifier",
]
