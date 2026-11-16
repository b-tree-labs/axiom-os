# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Freetext extractor — processes .md and .txt files dropped in inbox.

If LLM is available via the gateway, extracts people, initiatives,
decisions, and actions. Otherwise creates a single raw Signal with
the full text.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from axiom.infra.gateway import Gateway

from ..correlator import Correlator
from ..models import Extraction, Signal
from .base import BaseExtractor


class FreetextExtractor(BaseExtractor):
    """Extract signals from arbitrary text files."""

    @property
    def name(self) -> str:
        return "freetext"

    def can_handle(self, path: Path) -> bool:
        return path.exists() and path.suffix in (".md", ".txt")

    def extract(self, source: Path, **kwargs) -> Extraction:
        gateway: Gateway | None = kwargs.get("gateway")
        correlator: Correlator | None = kwargs.get("correlator")

        try:
            text = source.read_text(encoding="utf-8")
        except Exception as e:
            return Extraction(
                extractor=self.name,
                source_file=str(source),
                errors=[f"Failed to read file: {e}"],
            )

        now = datetime.now(UTC).isoformat()
        signals: list[Signal] = []

        if gateway and gateway.available:
            # Use LLM to extract structured signals
            signals.extend(self._extract_with_llm(text, source, gateway, correlator, now))
        else:
            # No LLM — preserve raw text
            signal = Signal(
                source=self.name,
                timestamp=now,
                raw_text=text[:2000],  # Truncate for storage
                signal_type="raw",
                detail=text[:500],
                confidence=0.3,
                metadata={"filename": source.name, "full_length": len(text)},
            )

            # Try to resolve any names/topics via correlator
            if correlator:
                signal.people = self._find_people_mentions(text, correlator)
                signal.initiatives = self._find_initiative_mentions(text, correlator)

            signals.append(signal)

        return Extraction(
            extractor=self.name,
            source_file=str(source),
            signals=signals,
        )

    def _extract_with_llm(
        self,
        text: str,
        source: Path,
        gateway: Gateway,
        correlator: Correlator | None,
        timestamp: str,
    ) -> list[Signal]:
        """Use LLM to extract structured signals from freetext."""
        import json as json_mod

        system = (
            "You are a signal extraction assistant for an engineering program. "
            "Extract structured signals from the provided text. "
            "Return a JSON array of objects, each with: "
            '"signal_type" (one of: progress, blocker, decision, action_item, status_change), '
            '"detail" (one-sentence summary), '
            '"people" (list of names mentioned), '
            '"initiatives" (list of project/initiative names mentioned). '
            "Be concise. Return only the JSON array."
        )

        response = gateway.complete(
            prompt=text[:4000],
            system=system,
            task="extraction",
        )

        if not response.success:
            return [Signal(
                source=self.name,
                timestamp=timestamp,
                raw_text=text[:2000],
                signal_type="raw",
                detail=f"LLM extraction failed: {response.error}. Raw text preserved.",
                confidence=0.3,
                metadata={"filename": source.name},
            )]

        # Parse LLM response
        signals = []
        try:
            # Try to extract JSON from response
            response_text = response.text.strip()
            # Handle markdown code fences
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                response_text = "\n".join(lines[1:-1])

            extracted = json_mod.loads(response_text)
            if not isinstance(extracted, list):
                extracted = [extracted]

            for item in extracted:
                people = item.get("people", [])
                initiatives = item.get("initiatives", [])

                # Resolve via correlator
                if correlator:
                    people = correlator.resolve_people(people)
                    initiatives = correlator.resolve_initiatives(initiatives)

                signals.append(Signal(
                    source=self.name,
                    timestamp=timestamp,
                    raw_text=text[:500],
                    people=people,
                    initiatives=initiatives,
                    signal_type=item.get("signal_type", "raw"),
                    detail=item.get("detail", ""),
                    confidence=0.7,
                    metadata={
                        "filename": source.name,
                        "llm_provider": response.provider,
                    },
                ))

        except (json_mod.JSONDecodeError, KeyError, TypeError):
            # LLM returned non-JSON — treat as raw
            signals.append(Signal(
                source=self.name,
                timestamp=timestamp,
                raw_text=text[:2000],
                signal_type="raw",
                detail=response.text[:500],
                confidence=0.5,
                metadata={
                    "filename": source.name,
                    "llm_provider": response.provider,
                },
            ))

        return signals

    @staticmethod
    def _find_people_mentions(text: str, correlator: Correlator) -> list[str]:
        """Find known people mentioned in text."""
        found = []
        text_lower = text.lower()
        for person in correlator.people:
            for key in person._match_keys:
                if key in text_lower and person.name not in found:
                    found.append(person.name)
                    break
        return found

    @staticmethod
    def _find_initiative_mentions(text: str, correlator: Correlator) -> list[str]:
        """Find known initiatives mentioned in text."""
        found = []
        text_lower = text.lower()
        for init in correlator.initiatives:
            # Use the full name as primary match
            if init.name.lower() in text_lower and init.name not in found:
                found.append(init.name)
                continue
            # Check keywords (but skip very short ones to avoid false positives)
            for key in init._match_keys:
                if len(key) > 3 and key in text_lower and init.name not in found:
                    found.append(init.name)
                    break
        return found
