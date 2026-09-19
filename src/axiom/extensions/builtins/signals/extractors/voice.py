# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Voice memo extractor — transcription via Whisper + optional diarization.

Processes .m4a files in inbox/raw/voice/:
1. Transcribe with openai-whisper (base model for speed)
2. Optionally run pyannote.audio for speaker diarization
3. Save transcript to inbox/processed/
4. Extract signals via LLM if available

Gracefully degrades if whisper is not installed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ..models import Extraction, Signal
from ..registry import SourceType, register_source
from .base import BaseExtractor


@register_source(
    name="voice",
    description="Voice memos from iPhone/macOS + video files",
    source_type=SourceType.PUSH,
    file_patterns=["*.m4a", "*.mp3", "*.wav", "*.webm", "*.mp4", "*.mov", "*.avi", "*.mkv"],
    icon="🎤",
    category="capture",
)
class VoiceExtractor(BaseExtractor):
    """Extract signals from voice memo recordings and video files."""

    # Audio formats
    AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".webm", ".ogg", ".aac", ".flac"}
    # Video formats (audio extracted via ffmpeg by whisper)
    VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
    # Combined
    SUPPORTED_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

    @property
    def name(self) -> str:
        return "voice"

    def can_handle(self, path: Path) -> bool:
        return path.exists() and path.suffix.lower() in self.SUPPORTED_EXTENSIONS

    def extract(self, source: Path, **kwargs) -> Extraction:
        """Transcribe a voice memo and extract signals.

        Args:
            source: Path to audio file.
            model_size: Whisper model size (default "base").
            gateway: LLM gateway for signal extraction.
            correlator: Entity correlator.
        """
        model_size = kwargs.get("model_size", "base")
        gateway = kwargs.get("gateway")
        correlator = kwargs.get("correlator")

        # Check if whisper is available
        try:
            import whisper  # type: ignore
        except ImportError:
            return Extraction(
                extractor=self.name,
                source_file=str(source),
                signals=[],
                errors=[
                    "openai-whisper not installed. Install with: pip install openai-whisper. "
                    "Skipping voice transcription."
                ],
            )

        now = datetime.now(UTC).isoformat()
        signals: list[Signal] = []
        errors: list[str] = []

        # Transcribe with word-level timestamps for accurate audio clip extraction
        try:
            print(f"  Transcribing {source.name} (model={model_size})...", flush=True)
            model = whisper.load_model(model_size)
            result = model.transcribe(str(source), word_timestamps=True)
            transcript: str = str(result.get("text", ""))
            segments: list[dict] = result.get("segments", [])  # type: ignore[assignment]

            if not transcript.strip():
                return Extraction(
                    extractor=self.name,
                    source_file=str(source),
                    errors=["Transcription produced empty text."],
                )

            print(f"  Transcribed: {len(transcript)} chars", flush=True)

        except Exception as e:
            return Extraction(
                extractor=self.name,
                source_file=str(source),
                errors=[f"Transcription failed: {e}"],
            )

        # Optional diarization + voice identification
        speakers: dict[str, list[str]] = {}
        speaker_matches: list = []

        try:
            hf_token = __import__("os").environ.get("HF_TOKEN")
            if hf_token:
                # Try voice identification (maps speakers to known people)
                try:
                    from ..voice_id import (
                        SpeakerIdentifier,
                        VoiceProfileStore,
                    )

                    # Get agents dir (relative to this file)
                    agents_dir = Path(__file__).parent.parent.parent
                    profiles = VoiceProfileStore(agents_dir)

                    if profiles.list_profiles():
                        print("  Identifying speakers...", flush=True)
                        identifier = SpeakerIdentifier(profiles)
                        speaker_matches = identifier.identify_with_consolidation(source)

                        # Build speaker timeline
                        for match in speaker_matches:
                            name = match.person_name or "Unknown"
                            speakers.setdefault(name, []).append(
                                f"[{match.start_time:.1f}s-{match.end_time:.1f}s]"
                            )

                        # Summary of identified vs unknown
                        identified = {m.person_name for m in speaker_matches if m.person_name}
                        print(f"  Identified: {identified or 'none'}", flush=True)
                except ImportError:
                    pass  # voice_id not available
                except Exception as e:
                    errors.append(f"Voice identification failed (non-fatal): {e}")

                # Fall back to basic diarization if no voice profiles
                if not speaker_matches:
                    from pyannote.audio import Pipeline  # type: ignore
                    print("  Running speaker diarization...", flush=True)
                    pipeline = Pipeline.from_pretrained(
                        "pyannote/speaker-diarization-3.1",
                        use_auth_token=hf_token,
                    )
                    diarization = pipeline(str(source))
                    for turn, _, speaker in diarization.itertracks(yield_label=True):
                        speakers.setdefault(speaker, []).append(
                            f"[{turn.start:.1f}s-{turn.end:.1f}s]"
                        )
                    print(f"  Found {len(speakers)} speaker(s)", flush=True)
        except ImportError:
            pass  # pyannote not available, skip diarization
        except Exception as e:
            errors.append(f"Diarization failed (non-fatal): {e}")

        # Save transcript to processed/
        processed_dir = self._get_processed_dir(source)
        processed_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = processed_dir / f"{source.stem}_transcript.md"

        transcript_content = f"# Transcript: {source.name}\n\n"
        transcript_content += f"**Transcribed:** {now}\n"
        transcript_content += f"**Model:** whisper-{model_size}\n\n"
        if speakers:
            transcript_content += "## Speakers\n\n"
            for speaker, spk_segments in speakers.items():
                transcript_content += f"- {speaker}: {len(spk_segments)} segments\n"
            transcript_content += "\n"
        transcript_content += "## Full Transcript\n\n"
        transcript_content += transcript

        transcript_path.write_text(transcript_content, encoding="utf-8")

        # Save word-level timestamps for accurate audio clip extraction
        # This enables precise clip extraction when reviewing corrections
        timestamps_path = processed_dir / f"{source.stem}_timestamps.json"
        try:
            import json

            word_timestamps = []
            for seg in segments:
                for word_info in seg.get("words", []):
                    word_timestamps.append({
                        "word": word_info.get("word", ""),
                        "start": word_info.get("start", 0),
                        "end": word_info.get("end", 0),
                    })

            timestamps_data = {
                "source_audio": str(source),
                "transcript_path": str(transcript_path),
                "created_at": now,
                "word_count": len(word_timestamps),
                "words": word_timestamps,
            }
            timestamps_path.write_text(json.dumps(timestamps_data, indent=2), encoding="utf-8")
            print(f"  Saved {len(word_timestamps)} word timestamps", flush=True)

            # Save speaker-annotated transcript if we identified speakers
            if speaker_matches:
                try:
                    from ..voice_id import annotate_transcript_with_speakers

                    annotated = annotate_transcript_with_speakers(
                        transcript, word_timestamps, speaker_matches
                    )
                    identified = {m.person_name for m in speaker_matches if m.person_name}

                    speakers_path = processed_dir / f"{source.stem}_speakers.md"
                    speaker_content = f"# Speaker-Annotated Transcript: {source.name}\n\n"
                    speaker_content += f"**Transcribed:** {now}\n"
                    speaker_content += f"**Speakers Identified:** {', '.join(identified) if identified else 'none'}\n\n"
                    speaker_content += "## Transcript\n\n"
                    speaker_content += annotated
                    speakers_path.write_text(speaker_content, encoding="utf-8")
                    print("  Saved speaker-annotated transcript", flush=True)
                except Exception as e:
                    errors.append(f"Failed to save speaker annotations (non-fatal): {e}")

        except Exception as e:
            errors.append(f"Failed to save word timestamps (non-fatal): {e}")

        # === AUTO-CORRECT TRANSCRIPT ===
        # Apply high-confidence corrections before signal extraction
        corrected_transcript = transcript  # fallback to original
        try:
            from ..corrector import TranscriptCorrector

            corrector = TranscriptCorrector()
            result = corrector.correct(transcript, transcript_path=str(transcript_path))

            if result.corrections:
                # Auto-apply corrections with confidence >= 0.85
                high_conf = [c for c in result.corrections if c.confidence >= 0.85]
                if high_conf:
                    # Apply corrections to transcript
                    corrected_transcript = transcript
                    for corr in high_conf:
                        idx = corrected_transcript.lower().find(corr.original.lower())
                        if idx != -1:
                            corrected_transcript = (
                                corrected_transcript[:idx] +
                                corr.corrected +
                                corrected_transcript[idx + len(corr.original):]
                            )

                    # Save corrected version
                    corrected_path = processed_dir / f"{source.stem}_corrected.md"
                    corrected_content = f"# Corrected Transcript: {source.name}\n\n"
                    corrected_content += f"**Transcribed:** {now}\n"
                    corrected_content += f"**Corrections Applied:** {len(high_conf)}\n\n"
                    corrected_content += "## Full Transcript\n\n"
                    corrected_content += corrected_transcript
                    corrected_path.write_text(corrected_content, encoding="utf-8")

                    # Save correction metadata
                    corrector.save_corrections(result,
                        processed_dir / f"{source.stem}_corrections.json")

                    print(f"    Applied {len(high_conf)} correction(s)", flush=True)
        except ImportError:
            pass  # corrector not available, use original
        except Exception as e:
            errors.append(f"Correction failed (non-fatal): {e}")

        # Create base signal
        signal = Signal(
            source=self.name,
            timestamp=now,
            raw_text=transcript[:2000],
            signal_type="raw",
            detail=f"Voice memo transcribed: {source.name} ({len(transcript)} chars)",
            confidence=0.6,
            metadata={
                "filename": source.name,
                "model": f"whisper-{model_size}",
                "transcript_path": str(transcript_path),
                "speaker_count": len(speakers),
            },
        )

        # Try LLM extraction (import here to avoid circular import at module level)
        # Use corrected_transcript for better signal extraction accuracy
        from .freetext import FreetextExtractor

        if gateway and gateway.available:
            ft = FreetextExtractor()
            llm_signals = ft._extract_with_llm(
                corrected_transcript, source, gateway, correlator, now
            )
            for s in llm_signals:
                s.source = self.name
                s.metadata["original_file"] = source.name
                s.metadata["used_corrected"] = corrected_transcript != transcript
            signals.extend(llm_signals)
        else:
            # No LLM — use correlator for basic matching
            if correlator:
                signal.people = FreetextExtractor._find_people_mentions(
                    corrected_transcript, correlator
                )
                signal.initiatives = FreetextExtractor._find_initiative_mentions(
                    corrected_transcript, correlator
                )
            signals.append(signal)

        return Extraction(
            extractor=self.name,
            source_file=str(source),
            signals=signals,
            errors=errors,
        )

    @staticmethod
    def _get_processed_dir(source: Path) -> Path:
        """Get the processed directory for saving transcripts."""
        # Navigate up from the source to find inbox/raw, then go to inbox/processed
        # source is like: tools/agents/inbox/raw/voice/memo.m4a
        parts = source.parts
        try:
            raw_idx = parts.index("raw")
            base = Path(*parts[:raw_idx])
            return base / "processed"
        except ValueError:
            return source.parent / "processed"
