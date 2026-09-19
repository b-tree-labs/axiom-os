# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Transcript correction using LangExtract.

Identifies likely transcription errors in voice memos using domain context
(people, initiatives, technical terms) and LLM-based extraction.

Usage:
    axi signal correct <transcript_path>
    axi signal correct --all
"""

from __future__ import annotations

import json
import os
import re
import textwrap
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

try:
    import langextract as lx
    LANGEXTRACT_AVAILABLE = True
except ImportError:
    LANGEXTRACT_AVAILABLE = False
    lx = None

from axiom.infra.state import LockedJsonFile

from .correlator import Correlator


@dataclass
class Correction:
    """A suggested correction for a transcription error."""
    original: str
    corrected: str
    category: str  # person_name, technical_term, acronym, facility, chemical
    confidence: float
    context: str  # surrounding text for review
    reason: str  # why this correction is suggested


@dataclass
class CorrectionResult:
    """Result of running transcript correction."""
    transcript_path: str
    corrections: list[Correction] = field(default_factory=list)
    glossary_size: int = 0
    model_used: str = ""
    timestamp: str = ""


class TranscriptCorrector:
    """Corrects transcription errors using domain context and LangExtract."""

    # Generic domain glossary - common STT mishearings (domain-agnostic)
    # Domain-specific terms should be added to runtime/config/stt_glossary.json
    _DEFAULT_GLOSSARY = {
        # Common technical mishearings
        "cfd": "CFD",
        "c of d": "CFD",
        "sea of dee": "CFD",
        "m dot": "ṁ (mass flow rate)",
        "redox": "redox",

        # Generic mishearings
        "the virus": "diverse",  # context-dependent

        # Common acronym expansions
        "doe": "DOE",
        "d o e": "DOE",
    }

    # Config file locations
    from axiom import REPO_ROOT as _REPO_ROOT
    _RUNTIME_DIR = _REPO_ROOT / "runtime"
    USER_GLOSSARY_PATH = _RUNTIME_DIR / "inbox" / "corrections" / "user_glossary.json"
    # Domain-specific configs (loaded from runtime/config/ if present)
    DOMAIN_GLOSSARY_PATH = _RUNTIME_DIR / "config" / "stt_glossary.json"
    DOMAIN_PROMPT_PATH = _RUNTIME_DIR / "config" / "stt_prompt.txt"
    DOMAIN_EXAMPLES_PATH = _RUNTIME_DIR / "config" / "stt_examples.json"

    @classmethod
    def _load_domain_glossary(cls) -> dict[str, str]:
        """Load domain glossary from config file, falling back to defaults."""
        glossary = dict(cls._DEFAULT_GLOSSARY)

        # Load domain-specific glossary from config
        if cls.DOMAIN_GLOSSARY_PATH.exists():
            try:
                import json
                data = json.loads(cls.DOMAIN_GLOSSARY_PATH.read_text())
                # Merge all sections
                for section in data.values():
                    if isinstance(section, dict):
                        for mishearing, correct in section.items():
                            glossary[mishearing.lower()] = correct
            except (json.JSONDecodeError, KeyError):
                pass  # Silently skip malformed glossary

        return glossary

    # Lazily loaded domain glossary
    DOMAIN_GLOSSARY: dict[str, str] | None = None

    def _load_static_examples(self) -> list:
        """Load few-shot examples from config or use generic defaults."""
        if not LANGEXTRACT_AVAILABLE or lx is None:
            return []

        # Try to load domain-specific examples from config
        if self.DOMAIN_EXAMPLES_PATH.exists():
            try:
                import json
                data = json.loads(self.DOMAIN_EXAMPLES_PATH.read_text())
                examples = []
                for ex in data.get("examples", []):
                    examples.append(lx.data.ExampleData(
                        text=ex["text"],
                        extractions=[
                            lx.data.Extraction(
                                extraction_class=ext["class"],
                                extraction_text=ext["text"],
                                attributes=ext.get("attributes", {})
                            ) for ext in ex.get("extractions", [])
                        ]
                    ))
                return examples
            except (json.JSONDecodeError, KeyError):
                pass  # Fall through to defaults

        # Generic default examples
        return [
            lx.data.ExampleData(
                text="Reluca is really hungry for us to start working with UCB",
                extractions=[
                    lx.data.Extraction(
                        extraction_class="person_name",
                        extraction_text="Reluca",
                        attributes={
                            "correction": "Raluca",
                            "confidence": "0.90",
                            "reason": "Name commonly misheard by speech-to-text"
                        }
                    )
                ]
            ),
            lx.data.ExampleData(
                text="On the CUVCU side, is it built and running?",
                extractions=[
                    lx.data.Extraction(
                        extraction_class="facility",
                        extraction_text="CUVCU",
                        attributes={
                            "correction": "CUCU",
                            "confidence": "0.75",
                            "reason": "Likely facility/system name - needs confirmation"
                        }
                    )
                ]
            ),
        ]

    def __init__(self, config_dir: Path | None = None):
        """Initialize with config directory for people/initiatives."""
        self.correlator = Correlator(config_dir)
        self._glossary = self._build_glossary()

    def _build_glossary(self) -> dict[str, str]:
        """Build glossary from domain terms + user glossary + people + initiatives."""
        # Lazy-load domain glossary on first use
        if TranscriptCorrector.DOMAIN_GLOSSARY is None:
            TranscriptCorrector.DOMAIN_GLOSSARY = self._load_domain_glossary()
        glossary = dict(TranscriptCorrector.DOMAIN_GLOSSARY)

        # Load user-defined glossary (highest priority)
        if self.USER_GLOSSARY_PATH.exists():
            user_data: dict = {}
            try:
                with LockedJsonFile(self.USER_GLOSSARY_PATH) as f:
                    user_data = f.read()
                # Merge all sections: terms, people, labs ("facilities" deprecated, use "labs")
                for section in ["terms", "people", "labs", "facilities"]:
                    if section in user_data:
                        for mishearing, correct in user_data[section].items():
                            glossary[mishearing.lower()] = correct
            except (json.JSONDecodeError, KeyError):
                pass  # Silently skip malformed glossary

        # Add people names and aliases (from people.md Aliases column)
        # This is critical for STT correction - aliases include nicknames and phonetic mishearings
        for person in self.correlator.people:
            # Add all aliases as glossary entries pointing to full name
            for alias in person.aliases:
                alias_lower = alias.lower().strip()
                if alias_lower and alias_lower != person.name.lower():
                    glossary[alias_lower] = person.name

            # Keep legacy hardcoded phonetic variants for backwards compatibility
            name_lower = person.name.lower()
            if "ondrej" in name_lower:
                glossary["on dray"] = person.name
                glossary["andre"] = person.name
            if "raluca" in name_lower:
                glossary["reluca"] = person.name
                glossary["reluka"] = person.name
                glossary["relukus"] = person.name
            if "chvala" in name_lower:
                glossary["shvala"] = person.name
                glossary["chavala"] = person.name
            if "jeongwon" in name_lower:
                glossary["john won"] = person.name
                glossary["jong won"] = person.name
            if "shahbazi" in name_lower:
                glossary["sha bazi"] = person.name
                glossary["shab azi"] = person.name

        # Add initiative names - common mishearings derived from initiative names
        for init in self.correlator.initiatives:
            name_lower = init.name.lower()
            # Add common phonetic mishearings for multi-word names
            if "digital twin" in name_lower:
                glossary["digital twing"] = "Digital Twin"
                glossary["digital twig"] = "Digital Twin"
            # Additional initiative-specific phonetics can be added via stt_glossary.json

        return glossary

    def _get_examples(self) -> list:
        """Build few-shot examples from approved human corrections + defaults."""
        if not LANGEXTRACT_AVAILABLE or lx is None:
            return []

        examples = []

        # First, add examples from human-approved corrections (reinforcement learning)
        try:
            from .correction_review import CorrectionReviewSystem
            review_system = CorrectionReviewSystem()
            training_examples = review_system.get_training_examples(limit=5)

            for ex in training_examples:
                examples.append(lx.data.ExampleData(
                    text=ex.context,
                    extractions=[
                        lx.data.Extraction(
                            extraction_class=ex.category,
                            extraction_text=ex.original,
                            attributes={
                                "correction": ex.corrected,
                                "confidence": "0.95",  # Human-approved = high confidence
                                "reason": ex.reason,
                            }
                        )
                    ]
                ))
        except Exception:
            pass  # Review system not initialized yet

        # Load domain-specific examples from config (or use generic defaults)
        static_examples = self._load_static_examples()

        # Prioritize human examples, cap at 10 total
        return (examples + static_examples)[:10]

    def _get_prompt(self) -> str:
        """Build the extraction prompt with domain context."""
        # Build people list with aliases for context
        people_with_aliases = []
        for p in self.correlator.people[:15]:
            if p.aliases:
                alias_str = "/".join(p.aliases[:3])  # First 3 aliases
                people_with_aliases.append(f"{p.name} (aka {alias_str})")
            else:
                people_with_aliases.append(p.name)
        people_list = ", ".join(people_with_aliases)
        init_list = ", ".join(i.name for i in self.correlator.initiatives[:10])

        # Try to load domain-specific prompt from config
        if self.DOMAIN_PROMPT_PATH.exists():
            prompt_template = self.DOMAIN_PROMPT_PATH.read_text()
            return prompt_template.format(people_list=people_list, init_list=init_list)

        # Default generic prompt
        return textwrap.dedent(f"""\
            You are correcting a transcript of a research meeting.

            CRITICAL: Person name corrections are HIGH PRIORITY for attribution/communication.
            Many names have nicknames or phonetic variants. Match mishearings to the correct full name.

            Identify likely transcription errors and suggest corrections. Focus on:
            1. Person names - HIGHEST PRIORITY (team members with aliases: {people_list})
            2. Technical terms
            3. Acronyms and abbreviations
            4. Facility/system names
            5. Chemical formulas or equations

            Active initiatives: {init_list}

            For each error found:
            - Extract the EXACT text as it appears (verbatim, no changes)
            - Provide the corrected version (use FULL NAME for people)
            - Rate confidence 0.0-1.0 (higher = more certain)
            - Explain why this is likely an error

            Only flag clear errors. Skip ambiguous cases or correctly transcribed text.
            Do not paraphrase - extract exact verbatim text for each error.""")

    def correct(
        self,
        transcript: str,
        model_id: str = "gemini-2.5-flash",
        extraction_passes: int = 2,
        transcript_path: str = "",
    ) -> CorrectionResult:
        """Run correction on a transcript.

        Args:
            transcript: The transcript text to correct.
            model_id: LLM model to use (default: gemini-2.5-flash).
            extraction_passes: Number of extraction passes for recall.
            transcript_path: Path to the source transcript file (for clip linking).

        Returns:
            CorrectionResult with suggested corrections.
        """
        self._current_transcript_path = transcript_path
        if not LANGEXTRACT_AVAILABLE:
            raise ImportError(
                "langextract not installed. Install with: pip install langextract"
            )

        # Check for API key
        api_key = os.environ.get("LANGEXTRACT_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key and "gemini" in model_id.lower():
            # Try to use existing Anthropic key with Claude instead
            anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
            if anthropic_key:
                # Fall back to our existing LLM gateway
                return self._correct_with_anthropic(transcript)
            raise ValueError(
                "No API key found. Set LANGEXTRACT_API_KEY or GOOGLE_API_KEY for Gemini, "
                "or ANTHROPIC_API_KEY for Claude fallback."
            )

        examples = self._get_examples()
        prompt = self._get_prompt()

        print(f"  Running LangExtract correction (model={model_id})...")
        print(f"  Glossary: {len(self._glossary)} terms")
        print(f"  Context: {len(self.correlator.people)} people, {len(self.correlator.initiatives)} initiatives")

        assert lx is not None  # Guaranteed by LANGEXTRACT_AVAILABLE check above
        result = lx.extract(
            text_or_documents=transcript,
            prompt_description=prompt,
            examples=examples,
            model_id=model_id,
            extraction_passes=extraction_passes,
            max_char_buffer=2000,  # Reasonable context window
        )

        corrections = []
        for doc in (result if isinstance(result, list) else [result]):
            for extraction in list(getattr(doc, 'extractions', [])):
                attrs = extraction.attributes or {}
                corrections.append(Correction(
                    original=extraction.extraction_text,
                    corrected=attrs.get("correction", ""),
                    category=extraction.extraction_class,
                    confidence=float(attrs.get("confidence", 0.5)),
                    context=self._get_context(transcript, extraction.extraction_text),
                    reason=attrs.get("reason", ""),
                ))

        return CorrectionResult(
            transcript_path=getattr(self, '_current_transcript_path', ''),
            corrections=corrections,
            glossary_size=len(self._glossary),
            model_used=model_id,
            timestamp=datetime.now(UTC).isoformat(),
        )

    def _correct_with_anthropic(self, transcript: str) -> CorrectionResult:
        """Fallback correction using Anthropic Claude via existing gateway."""
        from axiom.infra.gateway import Gateway

        gateway = Gateway()
        if not gateway.available:
            raise ValueError("No LLM available for correction")

        prompt = self._get_prompt()

        # Build examples into the prompt (generic examples)
        example_text = """
Examples of corrections:
- "Reluca" → "Raluca" (person name, confidence: 0.90)
- "c of d" → "CFD" (acronym, confidence: 0.92)
- "m dot" → "ṁ (mass flow rate)" (technical term, confidence: 0.85)
"""

        full_prompt = f"""{prompt}

{example_text}

Respond with a JSON array of corrections:
[
  {{
    "original": "exact text from transcript",
    "corrected": "corrected version",
    "category": "person_name|technical_term|acronym|facility|chemical",
    "confidence": 0.85,
    "reason": "explanation"
  }}
]

Transcript to correct:
---
{transcript[:8000]}
---

Return ONLY the JSON array, no other text."""

        provider = gateway.active_provider
        print(f"  Running correction with {provider.name if provider else 'unknown'}...")
        print(f"  Glossary: {len(self._glossary)} terms")

        gateway_response = gateway.complete(full_prompt)
        response_text = gateway_response.text if hasattr(gateway_response, 'text') else str(gateway_response)

        # Parse JSON response
        corrections = []
        try:
            # Extract JSON from response
            import re
            json_match = re.search(r'\[.*\]', response_text, re.DOTALL)
            if json_match:
                items = json.loads(json_match.group())
                for item in items:
                    corrections.append(Correction(
                        original=item.get("original", ""),
                        corrected=item.get("corrected", ""),
                        category=item.get("category", "unknown"),
                        confidence=float(item.get("confidence", 0.5)),
                        context=self._get_context(transcript, item.get("original", "")),
                        reason=item.get("reason", ""),
                    ))
        except (json.JSONDecodeError, ValueError) as e:
            print(f"  Warning: Could not parse LLM response: {e}")

        provider = gateway.active_provider
        return CorrectionResult(
            transcript_path=getattr(self, '_current_transcript_path', ''),
            corrections=corrections,
            glossary_size=len(self._glossary),
            model_used=provider.model if provider else "unknown",
            timestamp=datetime.now(UTC).isoformat(),
        )

    def _get_context(self, transcript: str, text: str, context_chars: int = 100) -> str:
        """Get surrounding context for a piece of text.

        Uses a three-tier fallback strategy:
        1. Case-insensitive exact match
        2. Partial word match (longest word in text)
        3. Fuzzy match via SequenceMatcher (ratio >= 0.6)
        """
        idx = transcript.lower().find(text.lower())

        # Fallback: try matching the longest word from the text
        if idx == -1:
            words = sorted(text.split(), key=len, reverse=True)
            for word in words:
                if len(word) < 3:
                    continue
                idx = transcript.lower().find(word.lower())
                if idx != -1:
                    break

        # Fallback: fuzzy match sliding window
        if idx == -1:
            from difflib import SequenceMatcher
            text_lower = text.lower()
            text_len = len(text)
            best_ratio = 0.0
            best_idx = -1
            transcript_lower = transcript.lower()
            for i in range(len(transcript_lower) - text_len + 1):
                window = transcript_lower[i:i + text_len]
                ratio = SequenceMatcher(None, text_lower, window).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_idx = i
            if best_ratio >= 0.6:
                idx = best_idx

        if idx == -1:
            return ""

        start = max(0, idx - context_chars)
        end = min(len(transcript), idx + len(text) + context_chars)
        context = transcript[start:end]
        if start > 0:
            context = "..." + context
        if end < len(transcript):
            context = context + "..."
        return context

    def correct_file(self, transcript_path: Path) -> CorrectionResult:
        """Correct a transcript file."""
        if not transcript_path.exists():
            raise FileNotFoundError(f"Transcript not found: {transcript_path}")

        content = transcript_path.read_text(encoding="utf-8")

        # Extract just the transcript text (skip metadata header)
        lines = content.split("\n")
        transcript_start = 0
        for i, line in enumerate(lines):
            if line.startswith("## Full Transcript"):
                transcript_start = i + 1
                break

        transcript = "\n".join(lines[transcript_start:])

        result = self.correct(transcript)
        result.transcript_path = str(transcript_path)
        return result

    def apply_corrections(
        self,
        transcript_path: Path,
        corrections: list[Correction],
        min_confidence: float = 0.8,
        add_paragraph_breaks: bool = True,
    ) -> str:
        """Apply high-confidence corrections to a transcript.

        Args:
            transcript_path: Path to the transcript file
            corrections: List of corrections to apply
            min_confidence: Minimum confidence threshold
            add_paragraph_breaks: If True, add paragraph breaks for readability

        Returns the corrected transcript text.
        """
        content = transcript_path.read_text(encoding="utf-8")

        # Apply corrections in reverse order of position to preserve indices
        for corr in sorted(
            [c for c in corrections if c.confidence >= min_confidence],
            key=lambda c: content.lower().find(c.original.lower()),
            reverse=True
        ):
            idx = content.lower().find(corr.original.lower())
            if idx != -1:
                # Preserve original case pattern if possible
                content = (
                    content[:idx] +
                    corr.corrected +
                    content[idx + len(corr.original):]
                )

        # Add paragraph breaks for readability (Whisper doesn't honor natural pauses)
        if add_paragraph_breaks:
            content = self._add_paragraph_breaks(content)

        return content

    def _add_paragraph_breaks(self, text: str) -> str:
        """Add paragraph breaks at natural points for readability.

        Whisper produces wall-of-text output. This adds breaks at:
        - Discourse markers that signal topic transitions
        - After certain sentence patterns (e.g., questions followed by statements)
        - Every ~150-200 words as a fallback
        """
        # Skip if already has paragraph breaks
        if "\n\n" in text:
            return text

        # Discourse markers that often start new topics
        TOPIC_MARKERS = [
            r'(?<=[.!?])\s+(So,?\s)',
            r'(?<=[.!?])\s+(Anyway,?\s)',
            r'(?<=[.!?])\s+(Now,?\s)',
            r'(?<=[.!?])\s+(Alright,?\s)',
            r'(?<=[.!?])\s+(All right,?\s)',
            r'(?<=[.!?])\s+(Okay,?\s)',
            r'(?<=[.!?])\s+(OK,?\s)',
            r'(?<=[.!?])\s+(Moving on,?\s)',
            r'(?<=[.!?])\s+(Next,?\s)',
            r'(?<=[.!?])\s+(Also,?\s)',
            r'(?<=[.!?])\s+(Additionally,?\s)',
            r'(?<=[.!?])\s+(Furthermore,?\s)',
            r'(?<=[.!?])\s+(On another note,?\s)',
            r'(?<=[.!?])\s+(Speaking of\s)',
            r'(?<=[.!?])\s+(By the way,?\s)',
            r'(?<=[.!?])\s+(One more thing,?\s)',
            r'(?<=[.!?])\s+(The other thing\s)',
        ]

        # Add breaks before discourse markers
        for pattern in TOPIC_MARKERS:
            text = re.sub(pattern, r'.\n\n\1', text)

        # Fallback: add breaks every ~200 words if no breaks added yet
        if "\n\n" not in text:
            sentences = re.split(r'(?<=[.!?])\s+', text)
            result = []
            word_count = 0
            for sentence in sentences:
                result.append(sentence)
                word_count += len(sentence.split())
                if word_count >= 180:
                    result.append("\n")
                    word_count = 0
            text = " ".join(result).replace(" \n ", "\n\n")

        return text

    def save_corrections(
        self,
        result: CorrectionResult,
        output_path: Path | None = None,
        add_to_review: bool = True,
    ) -> Path:
        """Save correction results to JSON and optionally queue for review.

        Args:
            result: CorrectionResult from correct() or correct_file()
            output_path: Optional output path for JSON
            add_to_review: If True, add corrections to human review queue
        """
        if output_path is None:
            base = Path(result.transcript_path)
            output_path = base.parent / f"{base.stem}_corrections.json"

        data = {
            "transcript_path": result.transcript_path,
            "timestamp": result.timestamp,
            "model_used": result.model_used,
            "glossary_size": result.glossary_size,
            "correction_count": len(result.corrections),
            "corrections": [
                {
                    "original": c.original,
                    "corrected": c.corrected,
                    "category": c.category,
                    "confidence": c.confidence,
                    "reason": c.reason,
                    "context": c.context,
                }
                for c in result.corrections
            ]
        }

        output_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        # Add corrections to human review queue for reinforcement learning
        if add_to_review:
            self._add_to_review_queue(result)

        return output_path

    def _add_to_review_queue(self, result: CorrectionResult) -> None:
        """Record applied corrections to audit trail for review.

        Note: Audio playback during review uses word-level timestamps from
        the *_timestamps.json files generated by Whisper. No clip extraction needed.
        """
        try:
            from axiom.infra.hash_utils import MEDIUM, fingerprint

            from .correction_review import CorrectionReviewSystem
            review_system = CorrectionReviewSystem()

            for corr in result.corrections:
                # Generate deterministic ID from content hash
                id_content = f"{result.transcript_path}|{corr.original}|{corr.corrected}|{corr.context}"
                content_hash = fingerprint(id_content, length=MEDIUM)
                correction_id = f"corr_{content_hash}"

                # Record all applied corrections (non-blocking audit trail)
                review_system.record_applied(
                    correction_id=correction_id,
                    transcript_path=result.transcript_path,
                    original=corr.original,
                    corrected=corr.corrected,
                    category=corr.category,
                    confidence=corr.confidence,
                    context=corr.context,
                    reason=corr.reason,
                )

        except Exception as e:
            # Don't fail correction if review system has issues
            print(f"  Warning: Could not record to audit trail: {e}")


def correct_transcript(
    transcript_path: Path,
    auto_apply: bool = False,
    min_confidence: float = 0.8,
) -> CorrectionResult:
    """Convenience function to correct a transcript file.

    Args:
        transcript_path: Path to transcript markdown file.
        auto_apply: If True, apply high-confidence corrections automatically.
        min_confidence: Minimum confidence to auto-apply (default 0.8).

    Returns:
        CorrectionResult with all suggested corrections.
    """
    corrector = TranscriptCorrector()
    result = corrector.correct_file(transcript_path)

    # Save corrections
    corrections_path = corrector.save_corrections(result)
    print(f"  Saved {len(result.corrections)} correction(s) to {corrections_path.name}")

    if auto_apply:
        high_conf = [c for c in result.corrections if c.confidence >= min_confidence]
        if high_conf:
            corrected_text = corrector.apply_corrections(
                transcript_path,
                result.corrections,
                min_confidence
            )
            # Save corrected version
            corrected_path = transcript_path.parent / f"{transcript_path.stem}_corrected.md"
            corrected_path.write_text(corrected_text, encoding="utf-8")
            print(f"  Applied {len(high_conf)} high-confidence corrections to {corrected_path.name}")

    return result
