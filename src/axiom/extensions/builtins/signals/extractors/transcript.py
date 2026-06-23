# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Transcript extractor — parses already-transcribed meeting notes.

Handles meeting transcripts that have already been converted to text
(e.g., from the meeting-intake pipeline). Extracts signals using
the LLM gateway if available, falls back to keyword matching.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from axiom.infra.gateway import Gateway

from ..correlator import Correlator
from ..models import Extraction, Signal
from ..registry import SourceType, register_source
from .base import BaseExtractor
from .freetext import FreetextExtractor


@register_source(
    name="teams_transcript",
    description="Teams meeting transcripts (VTT/SRT/text)",
    source_type=SourceType.PUSH,
    inbox_subdir="teams",
    file_patterns=["*.vtt", "*.srt", "*.md", "*.txt"],
    icon="📝",
    category="meetings",
)
class TranscriptExtractor(BaseExtractor):
    """Extract signals from pre-existing meeting transcripts."""

    @property
    def name(self) -> str:
        return "transcript"

    SUPPORTED_EXTENSIONS = {".md", ".txt", ".vtt", ".srt"}

    def can_handle(self, path: Path) -> bool:
        if not path.exists():
            return False
        # Accept transcript files by extension or location
        name_lower = path.name.lower()
        in_teams_dir = "teams" in str(path).lower()
        is_transcript_name = (
            "transcript" in name_lower
            or "meeting" in name_lower
        )
        is_supported_ext = path.suffix.lower() in self.SUPPORTED_EXTENSIONS

        # VTT/SRT files are always transcripts
        if path.suffix.lower() in (".vtt", ".srt"):
            return True
        # Other files need to be in teams/ or have transcript-related names
        return is_supported_ext and (in_teams_dir or is_transcript_name)

    @staticmethod
    def _parse_vtt_srt(content: str) -> str:
        """Extract plain text from VTT or SRT subtitle format.

        Removes timestamps, cue identifiers, and styling tags.
        """
        import re
        lines = content.split('\n')
        text_lines = []

        for line in lines:
            line = line.strip()
            # Skip WEBVTT header
            if line.startswith('WEBVTT') or line.startswith('NOTE'):
                continue
            # Skip cue identifiers (numbers or timestamps)
            if re.match(r'^\d+$', line):
                continue
            if re.match(r'^\d{2}:\d{2}', line):  # Timestamp line
                continue
            if '-->' in line:  # Timestamp range
                continue
            if not line:
                continue
            # Remove VTT styling tags like <v Speaker>
            line = re.sub(r'<[^>]+>', '', line)
            # Remove speaker labels like "Speaker 1:" at start
            line = re.sub(r'^[A-Za-z\s]+\d*:\s*', '', line)
            if line:
                text_lines.append(line)

        return ' '.join(text_lines)

    def extract(self, source: Path, **kwargs) -> Extraction:
        """Extract signals from a meeting transcript.

        Delegates to FreetextExtractor with a meeting-specific system prompt
        when LLM is available.
        """
        gateway: Gateway | None = kwargs.get("gateway")
        correlator: Correlator | None = kwargs.get("correlator")

        try:
            raw_content = source.read_text(encoding="utf-8")
            # Parse VTT/SRT files to extract plain text
            if source.suffix.lower() in (".vtt", ".srt"):
                text = self._parse_vtt_srt(raw_content)
                print(f"    Parsed {source.suffix} → {len(text)} chars of text", flush=True)
            else:
                text = raw_content
        except Exception as e:
            return Extraction(
                extractor=self.name,
                source_file=str(source),
                errors=[f"Failed to read transcript: {e}"],
            )

        now = datetime.now(UTC).isoformat()
        signals: list[Signal] = []
        errors: list[str] = []

        # === AUTO-CORRECT TRANSCRIPT ===
        # Apply high-confidence corrections before signal extraction
        corrected_text = text
        corrections_applied = 0
        try:
            from ..corrector import TranscriptCorrector

            corrector = TranscriptCorrector()
            result = corrector.correct(text)

            if result.corrections:
                high_conf = [c for c in result.corrections if c.confidence >= 0.85]
                if high_conf:
                    for corr in high_conf:
                        idx = corrected_text.lower().find(corr.original.lower())
                        if idx != -1:
                            corrected_text = (
                                corrected_text[:idx] +
                                corr.corrected +
                                corrected_text[idx + len(corr.original):]
                            )
                            corrections_applied += 1

                    # Save corrected version alongside original
                    corrected_path = source.parent / f"{source.stem}_corrected{source.suffix}"
                    corrected_path.write_text(corrected_text, encoding="utf-8")

                    # Save correction metadata
                    result.transcript_path = str(source)
                    corrector.save_corrections(result,
                        source.parent / f"{source.stem}_corrections.json")

                    print(f"    Applied {corrections_applied} correction(s)", flush=True)
        except ImportError:
            pass  # corrector not available
        except Exception as e:
            errors.append(f"Correction failed (non-fatal): {e}")

        if gateway and gateway.available:
            signals = self._extract_with_meeting_prompt(
                corrected_text, source, gateway, correlator, now
            )
            # Add metadata about corrections
            for s in signals:
                s.metadata["corrections_applied"] = corrections_applied
                s.metadata["used_corrected"] = corrections_applied > 0
        else:
            # Fall back to basic keyword matching
            ft = FreetextExtractor()
            signal = Signal(
                source=self.name,
                timestamp=now,
                raw_text=corrected_text[:2000],
                signal_type="raw",
                detail=f"Meeting transcript: {source.name} ({len(corrected_text)} chars)",
                confidence=0.3,
                metadata={
                    "filename": source.name,
                    "full_length": len(corrected_text),
                    "corrections_applied": corrections_applied,
                },
            )
            if correlator:
                signal.people = ft._find_people_mentions(corrected_text, correlator)
                signal.initiatives = ft._find_initiative_mentions(corrected_text, correlator)
            signals.append(signal)

        return Extraction(
            extractor=self.name,
            source_file=str(source),
            signals=signals,
            errors=errors,
        )

    def _extract_with_meeting_prompt(
        self,
        text: str,
        source: Path,
        gateway: Gateway,
        correlator: Correlator | None,
        timestamp: str,
    ) -> list[Signal]:
        """Use LLM with meeting-specific prompt."""
        import json as json_mod

        system = (
            "You are analyzing a meeting transcript for an engineering program. "
            "Extract structured signals. Focus on: decisions made, action items assigned, "
            "blockers raised, progress updates, and status changes. "
            "Return a JSON array of objects, each with: "
            '"signal_type" (one of: progress, blocker, decision, action_item, status_change), '
            '"detail" (one-sentence summary), '
            '"people" (list of names mentioned or responsible), '
            '"initiatives" (list of project/initiative names discussed). '
            "Be thorough — meetings often contain multiple signals. Return only the JSON array."
        )

        response = gateway.complete(
            prompt=text[:6000],
            system=system,
            task="extraction",
        )

        if not response.success:
            return [Signal(
                source=self.name,
                timestamp=timestamp,
                raw_text=text[:2000],
                signal_type="raw",
                detail="Meeting LLM extraction failed. Raw text preserved.",
                confidence=0.3,
                metadata={"filename": source.name},
            )]

        signals = []
        try:
            response_text = response.text.strip()
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                response_text = "\n".join(lines[1:-1])

            extracted = json_mod.loads(response_text)
            if not isinstance(extracted, list):
                extracted = [extracted]

            for item in extracted:
                people = item.get("people", [])
                initiatives = item.get("initiatives", [])

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
            signals.append(Signal(
                source=self.name,
                timestamp=timestamp,
                raw_text=text[:2000],
                signal_type="raw",
                detail=response.text[:500],
                confidence=0.5,
                metadata={"filename": source.name, "llm_provider": response.provider},
            ))

        return signals
