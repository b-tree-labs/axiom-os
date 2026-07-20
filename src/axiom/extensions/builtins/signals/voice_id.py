# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Voice Identification — Tag speakers with known identities.

Enables automatic speaker identification in voice recordings by:
1. Enrolling known voices (extracting and storing speaker embeddings)
2. Identifying speakers in new recordings (matching against enrolled profiles)
3. Linking identified speakers to team members (from people.md)

Architecture:
- Voice profiles stored in inbox/voice_profiles/
- Each profile contains speaker embeddings (vector) + metadata
- Uses pyannote.audio for speaker embedding extraction
- Cosine similarity for speaker matching

Usage:
    # Enroll a voice from an audio file
    profiles = VoiceProfileStore(agents_dir)
    profiles.enroll("Ben Booth", audio_path, start_time=0, end_time=30)

    # Identify speakers in a new recording
    identifier = SpeakerIdentifier(profiles)
    speakers = identifier.identify(new_audio_path)
    # Returns: [("Ben Booth", 0.95, 0.0, 15.2), ("Unknown", 0.0, 15.2, 45.0)]

Dependencies (install as needed):
    pip install pyannote.audio torch torchaudio
    # Requires HF_TOKEN env var for pyannote models
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from axiom.infra.state import atomic_write

if TYPE_CHECKING:
    pass


@dataclass
class VoiceProfile:
    """A voice profile for a known person.

    Stores speaker embeddings extracted from audio samples.
    Multiple embeddings can be stored per person (different recording conditions).
    """
    person_name: str  # Full name matching people.md
    embeddings: list[list[float]]  # List of embedding vectors
    created_at: str
    updated_at: str
    sample_count: int = 0  # Number of audio samples used
    sample_sources: list[str] = field(default_factory=list)  # Audio file paths
    metadata: dict = field(default_factory=dict)

    @property
    def average_embedding(self) -> list[float]:
        """Get average embedding across all samples."""
        if not self.embeddings:
            return []
        arr = np.array(self.embeddings)
        avg = np.mean(arr, axis=0)
        # L2 normalize
        norm = np.linalg.norm(avg)
        if norm > 0:
            avg = avg / norm
        return avg.tolist()

    def to_dict(self) -> dict:
        return {
            "person_name": self.person_name,
            "embeddings": self.embeddings,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "sample_count": self.sample_count,
            "sample_sources": self.sample_sources,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> VoiceProfile:
        return cls(
            person_name=data["person_name"],
            embeddings=data.get("embeddings", []),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            sample_count=data.get("sample_count", 0),
            sample_sources=data.get("sample_sources", []),
            metadata=data.get("metadata", {}),
        )


class VoiceProfileStore:
    """Storage for voice profiles.

    Profiles are stored in inbox/voice_profiles/ as JSON files.
    """

    def __init__(self, agents_dir: Path | str):
        self.agents_dir = Path(agents_dir)
        self.profiles_dir = self.agents_dir / "inbox" / "voice_profiles"
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self._profiles: dict[str, VoiceProfile] = {}
        self._embedding_model = None
        self._load_profiles()

    def _load_profiles(self) -> None:
        """Load all voice profiles from disk."""
        for profile_file in self.profiles_dir.glob("*.json"):
            try:
                data = json.loads(profile_file.read_text())
                profile = VoiceProfile.from_dict(data)
                self._profiles[profile.person_name.lower()] = profile
            except Exception as e:
                print(f"Warning: Failed to load profile {profile_file}: {e}")

    def _save_profile(self, profile: VoiceProfile) -> None:
        """Save a profile to disk."""
        # Sanitize filename
        safe_name = profile.person_name.replace(" ", "_").lower()
        safe_name = "".join(c for c in safe_name if c.isalnum() or c == "_")
        path = self.profiles_dir / f"{safe_name}.json"
        atomic_write(path, profile.to_dict())

    def _get_embedding_model(self):
        """Lazy-load the embedding model."""
        if self._embedding_model is None:
            try:
                import os

                from pyannote.audio import Model

                from axiom.infra.connections import get_credential
                hf_token = get_credential("huggingface") or os.environ.get("HF_TOKEN")
                if not hf_token:
                    raise RuntimeError(
                        "HF_TOKEN required for speaker embedding — run: axi connect huggingface\n"
                        "Get one at https://huggingface.co/settings/tokens"
                    )

                # Use pyannote's speaker embedding model
                print("Loading speaker embedding model...", flush=True)
                self._embedding_model = Model.from_pretrained(
                    "pyannote/embedding",
                    use_auth_token=hf_token
                )
                print("Speaker embedding model loaded.", flush=True)
            except ImportError:
                raise RuntimeError(
                    "pyannote.audio required for voice identification. "
                    "Install with: pip install pyannote.audio torch torchaudio"
                )
        return self._embedding_model

    def extract_embedding(
        self,
        audio_path: Path | str,
        start_time: float | None = None,
        end_time: float | None = None
    ) -> list[float]:
        """Extract speaker embedding from audio segment.

        Args:
            audio_path: Path to audio file
            start_time: Start time in seconds (optional)
            end_time: End time in seconds (optional)

        Returns:
            Embedding vector (512-dim for pyannote)
        """
        import torchaudio
        from pyannote.audio import Inference

        model = self._get_embedding_model()
        inference = Inference(model, window="whole")

        audio_path = Path(audio_path)

        # Load audio
        if start_time is not None or end_time is not None:
            # Load segment
            waveform, sample_rate = torchaudio.load(str(audio_path))
            start_sample = int((start_time or 0) * sample_rate)
            end_sample = int((end_time or waveform.shape[1] / sample_rate) * sample_rate)
            waveform = waveform[:, start_sample:end_sample]

            # Save to temp file for inference
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                torchaudio.save(f.name, waveform, sample_rate)
                embedding = inference(f.name)
                Path(f.name).unlink()
        else:
            embedding = inference(str(audio_path))

        # Convert to list (embedding is numpy array)
        return embedding.flatten().tolist()

    def enroll(
        self,
        person_name: str,
        audio_path: Path | str,
        start_time: float | None = None,
        end_time: float | None = None,
    ) -> VoiceProfile:
        """Enroll a voice sample for a person.

        Can be called multiple times to add more samples.
        More samples = better identification accuracy.

        Args:
            person_name: Full name (should match people.md)
            audio_path: Path to audio file containing their voice
            start_time: Start of voice segment (seconds)
            end_time: End of voice segment (seconds)

        Returns:
            Updated VoiceProfile
        """
        audio_path = Path(audio_path)
        now = datetime.now(UTC).isoformat()

        # Extract embedding
        print(f"Extracting voice embedding for {person_name}...", flush=True)
        embedding = self.extract_embedding(audio_path, start_time, end_time)

        # Get or create profile
        key = person_name.lower()
        if key in self._profiles:
            profile = self._profiles[key]
            profile.embeddings.append(embedding)
            profile.sample_count += 1
            profile.sample_sources.append(str(audio_path))
            profile.updated_at = now
        else:
            profile = VoiceProfile(
                person_name=person_name,
                embeddings=[embedding],
                created_at=now,
                updated_at=now,
                sample_count=1,
                sample_sources=[str(audio_path)],
            )
            self._profiles[key] = profile

        self._save_profile(profile)
        print(f"Enrolled voice for {person_name} ({profile.sample_count} samples)", flush=True)
        return profile

    def get_profile(self, person_name: str) -> VoiceProfile | None:
        """Get a voice profile by name."""
        return self._profiles.get(person_name.lower())

    def list_profiles(self) -> list[VoiceProfile]:
        """List all enrolled voice profiles."""
        return list(self._profiles.values())

    def delete_profile(self, person_name: str) -> bool:
        """Delete a voice profile."""
        key = person_name.lower()
        if key in self._profiles:
            profile = self._profiles.pop(key)
            safe_name = profile.person_name.replace(" ", "_").lower()
            safe_name = "".join(c for c in safe_name if c.isalnum() or c == "_")
            path = self.profiles_dir / f"{safe_name}.json"
            if path.exists():
                path.unlink()
            return True
        return False


@dataclass
class SpeakerMatch:
    """A matched speaker segment."""
    person_name: str | None  # None if unknown
    confidence: float  # 0.0-1.0
    start_time: float  # seconds
    end_time: float  # seconds
    embedding: list[float] | None = None  # For potential enrollment


class SpeakerIdentifier:
    """Identifies speakers in audio recordings.

    Uses speaker diarization + embedding matching to identify
    who is speaking at each moment in a recording.
    """

    # Minimum similarity score to consider a match
    MATCH_THRESHOLD = 0.7  # Cosine similarity

    def __init__(self, profile_store: VoiceProfileStore):
        self.profiles = profile_store
        self._diarization_pipeline = None

    def _get_diarization_pipeline(self):
        """Lazy-load diarization pipeline."""
        if self._diarization_pipeline is None:
            import os

            from pyannote.audio import Pipeline

            from axiom.infra.connections import get_credential
            hf_token = get_credential("huggingface") or os.environ.get("HF_TOKEN")
            if not hf_token:
                raise RuntimeError("HF_TOKEN required — run: axi connect huggingface")

            print("Loading diarization pipeline...", flush=True)
            self._diarization_pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                use_auth_token=hf_token
            )
            print("Diarization pipeline loaded.", flush=True)
        return self._diarization_pipeline

    def _cosine_similarity(self, a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two embeddings."""
        a_arr = np.array(a)
        b_arr = np.array(b)
        dot = np.dot(a_arr, b_arr)
        norm_a = np.linalg.norm(a_arr)
        norm_b = np.linalg.norm(b_arr)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(dot / (norm_a * norm_b))

    def _match_embedding(self, embedding: list[float]) -> tuple[str | None, float]:
        """Match an embedding against enrolled profiles.

        Returns:
            (person_name, confidence) or (None, 0.0) if no match
        """
        best_match = None
        best_score = 0.0

        for profile in self.profiles.list_profiles():
            avg_embedding = profile.average_embedding
            if not avg_embedding:
                continue

            similarity = self._cosine_similarity(embedding, avg_embedding)
            if similarity > best_score:
                best_score = similarity
                best_match = profile.person_name

        if best_score >= self.MATCH_THRESHOLD:
            return best_match, best_score
        return None, best_score

    def identify(
        self,
        audio_path: Path | str,
        min_segment_duration: float = 1.0
    ) -> list[SpeakerMatch]:
        """Identify speakers in an audio recording.

        Args:
            audio_path: Path to audio file
            min_segment_duration: Minimum segment length to process (seconds)

        Returns:
            List of SpeakerMatch objects with identified speakers
        """
        audio_path = Path(audio_path)
        print(f"Identifying speakers in {audio_path.name}...", flush=True)

        # Run diarization
        pipeline = self._get_diarization_pipeline()
        diarization = pipeline(str(audio_path))

        matches: list[SpeakerMatch] = []
        speaker_embeddings: dict[str, list[float]] = {}  # Cache per diarization label

        for turn, _, speaker_label in diarization.itertracks(yield_label=True):
            duration = turn.end - turn.start
            if duration < min_segment_duration:
                continue

            # Get or compute embedding for this speaker
            if speaker_label not in speaker_embeddings:
                try:
                    embedding = self.profiles.extract_embedding(
                        audio_path,
                        start_time=turn.start,
                        end_time=turn.end
                    )
                    speaker_embeddings[speaker_label] = embedding
                except Exception as e:
                    print(f"Warning: Failed to extract embedding for {speaker_label}: {e}")
                    continue
            else:
                embedding = speaker_embeddings[speaker_label]

            # Match against enrolled profiles
            person_name, confidence = self._match_embedding(embedding)

            matches.append(SpeakerMatch(
                person_name=person_name,
                confidence=confidence,
                start_time=turn.start,
                end_time=turn.end,
                embedding=embedding if person_name is None else None,
            ))

        # Summarize
        identified = {m.person_name for m in matches if m.person_name}
        unknown = sum(1 for m in matches if m.person_name is None)
        print(f"Identified: {identified or 'none'} | Unknown speakers: {unknown}", flush=True)

        return matches

    def identify_with_consolidation(
        self,
        audio_path: Path | str,
        min_segment_duration: float = 1.0
    ) -> list[SpeakerMatch]:
        """Identify speakers and consolidate consecutive segments.

        Merges consecutive segments from the same speaker.
        """
        matches = self.identify(audio_path, min_segment_duration)

        if not matches:
            return []

        # Sort by start time
        matches.sort(key=lambda m: m.start_time)

        # Consolidate
        consolidated: list[SpeakerMatch] = []
        current = matches[0]

        for match in matches[1:]:
            # Same speaker and close in time (within 0.5s gap)
            if (match.person_name == current.person_name and
                match.start_time - current.end_time < 0.5):
                # Extend current segment
                current = SpeakerMatch(
                    person_name=current.person_name,
                    confidence=max(current.confidence, match.confidence),
                    start_time=current.start_time,
                    end_time=match.end_time,
                )
            else:
                consolidated.append(current)
                current = match

        consolidated.append(current)
        return consolidated


def format_speaker_timeline(matches: list[SpeakerMatch]) -> str:
    """Format speaker matches as a readable timeline."""
    lines = ["## Speaker Timeline\n"]
    for m in sorted(matches, key=lambda x: x.start_time):
        name = m.person_name or f"Unknown (similarity: {m.confidence:.2f})"
        lines.append(f"- [{m.start_time:.1f}s - {m.end_time:.1f}s] **{name}**")
    return "\n".join(lines)


def annotate_transcript_with_speakers(
    transcript: str,
    word_timestamps: list[dict],
    speaker_matches: list[SpeakerMatch],
) -> str:
    """Annotate a transcript with speaker labels.

    Args:
        transcript: Original transcript text
        word_timestamps: List of {word, start, end} dicts
        speaker_matches: Identified speaker segments

    Returns:
        Transcript with speaker labels inserted
    """
    if not speaker_matches or not word_timestamps:
        return transcript

    # Sort matches by time
    sorted_matches = sorted(speaker_matches, key=lambda m: m.start_time)

    # Assign each word to a speaker
    annotated_words = []
    current_speaker = None

    for word_info in word_timestamps:
        word_time = word_info.get("start", 0)
        word_text = word_info.get("word", "")

        # Find speaker for this word
        speaker = None
        for match in sorted_matches:
            if match.start_time <= word_time <= match.end_time:
                speaker = match.person_name or "Unknown"
                break

        # Add speaker change marker
        if speaker != current_speaker:
            if current_speaker is not None:
                annotated_words.append("\n\n")
            annotated_words.append(f"**[{speaker}]:** ")
            current_speaker = speaker

        annotated_words.append(word_text)

    return "".join(annotated_words)
