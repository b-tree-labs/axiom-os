# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Media Library — Personal database of all recorded audio and video.

Indexes all recordings with transcripts for semantic search and playback.
Supports:
- Hybrid search (keyword + semantic with auto-detection)
- Segment extraction for sharing in PRDs
- Continuous RAG enrichment from cleaned transcripts
- Video and audio playback

Usage:
    from axiom.extensions.builtins.signals.media_library import MediaLibrary

    library = MediaLibrary()
    library.rebuild_index()  # Index all recordings

    # Search with auto-detection
    results = library.search("conversation about heat exchangers")

    # Play a segment
    library.play(results[0].media_id, start_sec=30, duration_sec=15)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from enum import Enum
from pathlib import Path
from typing import Literal

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.hash_utils import LONG, fingerprint
from axiom.infra.state import atomic_write

_RUNTIME_DIR = _REPO_ROOT / "runtime"
MEDIA_INDEX_PATH = _RUNTIME_DIR / "inbox" / "cache" / "media_index.json"
MEDIA_EMBEDDINGS_PATH = _RUNTIME_DIR / "inbox" / "cache" / "media_embeddings.json"
RAW_VOICE_DIR = _RUNTIME_DIR / "inbox" / "raw" / "voice"
RAW_VIDEO_DIR = _RUNTIME_DIR / "inbox" / "raw" / "video"
PROCESSED_DIR = _RUNTIME_DIR / "inbox" / "processed"


class MediaType(str, Enum):
    AUDIO = "audio"
    VIDEO = "video"


class SearchMode(str, Enum):
    KEYWORD = "keyword"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"
    AUTO = "auto"


class AccessLevel(str, Enum):
    """Access levels for shared media."""
    OWNER = "owner"          # Full control (delete, share, edit metadata)
    PARTICIPANT = "participant"  # View, play, search (was in the recording)
    SHARED = "shared"        # Explicitly shared by owner
    NONE = "none"


@dataclass
class Participant:
    """A person detected in or mentioned in a recording."""

    person_id: str  # Matches config/people.md identifier
    name: str
    role: str = ""  # "speaker", "mentioned", "attendee"
    access_level: AccessLevel = AccessLevel.PARTICIPANT
    mention_count: int = 0

    def to_dict(self) -> dict:
        return {
            "person_id": self.person_id,
            "name": self.name,
            "role": self.role,
            "access_level": self.access_level.value,
            "mention_count": self.mention_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Participant:
        return cls(
            person_id=data.get("person_id", ""),
            name=data.get("name", ""),
            role=data.get("role", ""),
            access_level=AccessLevel(data.get("access_level", "participant")),
            mention_count=data.get("mention_count", 0),
        )


@dataclass
class MediaItem:
    """A recorded audio or video item in the library.

    Implements participant-based access control:
    - Owner: The person who made the recording (full control)
    - Participants: People detected in the recording (automatic view access)
    - Shared: People explicitly granted access by owner

    This aligns with:
    - [Agent State Management PRD US-021](../docs/prds/prd-agent-state-management.md)
    - [Data Architecture Spec § 9](../docs/specs/spec-data-architecture.md)
    """

    media_id: str
    path: str
    media_type: MediaType
    title: str
    transcript_path: str | None = None
    timestamps_path: str | None = None
    duration_sec: float = 0.0
    recorded_at: str = ""
    word_count: int = 0
    transcript_preview: str = ""  # First ~200 chars

    # Ownership and access control
    owner_id: str = ""  # Person who made the recording
    participants: list[Participant] = field(default_factory=list)

    # For RAG indexing
    full_transcript: str = ""
    embedding: list[float] | None = None

    def to_dict(self) -> dict:
        return {
            "media_id": self.media_id,
            "path": self.path,
            "media_type": self.media_type.value,
            "title": self.title,
            "transcript_path": self.transcript_path,
            "timestamps_path": self.timestamps_path,
            "duration_sec": self.duration_sec,
            "recorded_at": self.recorded_at,
            "word_count": self.word_count,
            "transcript_preview": self.transcript_preview,
            "full_transcript": self.full_transcript,
            "owner_id": self.owner_id,
            "participants": [p.to_dict() for p in self.participants],
        }

    @classmethod
    def from_dict(cls, data: dict) -> MediaItem:
        participants = [
            Participant.from_dict(p) for p in data.get("participants", [])
        ]
        return cls(
            media_id=data["media_id"],
            path=data["path"],
            media_type=MediaType(data.get("media_type", "audio")),
            title=data.get("title", ""),
            transcript_path=data.get("transcript_path"),
            timestamps_path=data.get("timestamps_path"),
            duration_sec=data.get("duration_sec", 0.0),
            recorded_at=data.get("recorded_at", ""),
            word_count=data.get("word_count", 0),
            transcript_preview=data.get("transcript_preview", ""),
            full_transcript=data.get("full_transcript", ""),
            owner_id=data.get("owner_id", ""),
            participants=participants,
        )

    def get_access_level(self, person_id: str) -> AccessLevel:
        """Get access level for a person."""
        if person_id == self.owner_id:
            return AccessLevel.OWNER
        for p in self.participants:
            if p.person_id == person_id:
                return p.access_level
        return AccessLevel.NONE

    def has_access(self, person_id: str) -> bool:
        """Check if person has any access to this media."""
        return self.get_access_level(person_id) != AccessLevel.NONE

    def grant_access(self, person_id: str, name: str, level: AccessLevel = AccessLevel.SHARED) -> None:
        """Grant access to a person (owner action)."""
        # Check if already a participant
        for p in self.participants:
            if p.person_id == person_id:
                p.access_level = level
                return
        # Add new participant
        self.participants.append(Participant(
            person_id=person_id,
            name=name,
            role="shared",
            access_level=level,
        ))

    def revoke_access(self, person_id: str) -> bool:
        """Revoke access from a person (owner action)."""
        for i, p in enumerate(self.participants):
            if p.person_id == person_id and p.role == "shared":
                del self.participants[i]
                return True
        return False


@dataclass
class SearchResult:
    """A search result from the media library."""

    item: MediaItem
    score: float
    match_type: SearchMode
    matched_text: str = ""
    start_time_sec: float | None = None
    end_time_sec: float | None = None

    def __repr__(self) -> str:
        return f"SearchResult({self.item.title}, score={self.score:.2f}, mode={self.match_type.value})"


class MediaLibrary:
    """Personal media library with hybrid search."""

    def __init__(
        self,
        index_path: Path | None = None,
        embeddings_path: Path | None = None,
    ):
        self.index_path = index_path or MEDIA_INDEX_PATH
        self.embeddings_path = embeddings_path or MEDIA_EMBEDDINGS_PATH

        self._items: dict[str, MediaItem] = {}
        self._embeddings: dict[str, list[float]] = {}
        self._embedder = None

        self._load_index()

    def _load_index(self) -> None:
        """Load index from disk."""
        if self.index_path.exists():
            try:
                data = json.loads(self.index_path.read_text())
                for item_data in data.get("items", []):
                    item = MediaItem.from_dict(item_data)
                    self._items[item.media_id] = item
            except (json.JSONDecodeError, KeyError):
                pass

        if self.embeddings_path.exists():
            try:
                self._embeddings = json.loads(self.embeddings_path.read_text())
            except json.JSONDecodeError:
                pass

    def _save_index(self) -> None:
        """Persist index to disk."""
        self.index_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "version": "1.0",
            "updated_at": datetime.now(UTC).isoformat(),
            "item_count": len(self._items),
            "items": [item.to_dict() for item in self._items.values()],
        }
        atomic_write(self.index_path, data)
        atomic_write(self.embeddings_path, self._embeddings)

    def _get_embedder(self):
        """Get embedding provider (lazy load)."""
        if self._embedder is None:
            from .signal_rag import KeywordEmbeddings, LocalEmbeddings, OpenAIEmbeddings

            openai = OpenAIEmbeddings()
            if openai.is_available():
                self._embedder = openai
            else:
                local = LocalEmbeddings()
                if local.is_available():
                    self._embedder = local
                else:
                    self._embedder = KeywordEmbeddings()

        return self._embedder

    def _generate_media_id(self, path: Path) -> str:
        """Generate deterministic ID from file path."""
        content = f"{path.name}|{path.stat().st_size}"
        return fingerprint(content, length=LONG)

    def _get_file_duration(self, path: Path) -> float:
        """Get duration of audio/video file using ffprobe."""
        if not shutil.which("ffprobe"):
            return 0.0

        try:
            cmd = [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return float(result.stdout.strip())
        except (subprocess.TimeoutExpired, ValueError):
            return 0.0

    def _find_transcript(self, media_path: Path) -> tuple[Path | None, Path | None]:
        """Find transcript and timestamps files for a media file."""
        stem = media_path.stem

        # Check processed directory
        transcript_path = PROCESSED_DIR / f"{stem}_transcript.md"
        timestamps_path = PROCESSED_DIR / f"{stem}_timestamps.json"

        if not transcript_path.exists():
            # Try without common suffixes
            for suffix in ["_corrected", "_cleaned"]:
                alt_transcript = PROCESSED_DIR / f"{stem}{suffix}_transcript.md"
                if alt_transcript.exists():
                    transcript_path = alt_transcript
                    break

        return (
            transcript_path if transcript_path.exists() else None,
            timestamps_path if timestamps_path.exists() else None,
        )

    def _load_people_registry(self) -> dict[str, dict]:
        """Load people from config/people.md.

        Returns dict mapping person_id -> {name, aliases, email, ...}
        """
        people_path = _RUNTIME_DIR.parent / "config" / "people.md"
        if not people_path.exists():
            return {}

        people = {}
        try:
            content = people_path.read_text(encoding="utf-8")

            # Parse markdown format: ## Person Name followed by key: value lines
            current_person = None
            current_data: dict = {}

            for line in content.split("\n"):
                line = line.strip()
                if line.startswith("## "):
                    # Save previous person
                    if current_person:
                        people[current_person] = current_data
                    # Start new person
                    name = line[3:].strip()
                    current_person = name.lower().replace(" ", "_")
                    current_data = {"name": name, "aliases": [name.lower()]}
                elif ":" in line and current_person:
                    key, value = line.split(":", 1)
                    key = key.strip().lower()
                    value = value.strip()
                    if key == "aliases":
                        current_data["aliases"].extend(
                            [a.strip().lower() for a in value.split(",")]
                        )
                    else:
                        current_data[key] = value

            # Save last person
            if current_person:
                people[current_person] = current_data

        except OSError:
            pass

        return people

    def _detect_participants(self, transcript: str, owner_id: str = "") -> list[Participant]:
        """Detect participants mentioned in a transcript.

        Uses the people registry to identify mentions by name or alias.
        Supports:
        - Direct name mentions ("talked to Kevin")
        - Quote attributions ('Kevin said "..."')
        - Meeting formats ("Attendees: A, B, C")
        """
        if not transcript:
            return []

        people = self._load_people_registry()
        if not people:
            return []

        transcript_lower = transcript.lower()
        participants = []

        for person_id, person_data in people.items():
            name = person_data.get("name", "")
            aliases = person_data.get("aliases", [name.lower()])

            mention_count = 0
            for alias in aliases:
                if len(alias) < 3:  # Skip very short aliases
                    continue
                # Count word-boundary matches
                import re
                pattern = r'\b' + re.escape(alias) + r'\b'
                matches = re.findall(pattern, transcript_lower)
                mention_count += len(matches)

            if mention_count > 0:
                # Determine role based on context
                role = "mentioned"

                # Check for speaker indicators
                speaker_patterns = [
                    f"{aliases[0]} said",
                    f"{aliases[0]} mentioned",
                    f"{aliases[0]}:",
                    f"from {aliases[0]}",
                    f"with {aliases[0]}",
                ]
                for pattern in speaker_patterns:
                    if pattern in transcript_lower:
                        role = "speaker"
                        break

                # Owner detection
                access_level = AccessLevel.PARTICIPANT
                if person_id == owner_id:
                    access_level = AccessLevel.OWNER

                participants.append(Participant(
                    person_id=person_id,
                    name=name,
                    role=role,
                    access_level=access_level,
                    mention_count=mention_count,
                ))

        # Sort by mention count (most mentioned first)
        participants.sort(key=lambda p: -p.mention_count)
        return participants

    def _index_media_file(self, path: Path, media_type: MediaType) -> MediaItem | None:
        """Index a single media file."""
        if not path.exists():
            return None

        media_id = self._generate_media_id(path)

        # Skip if already indexed with same path
        if media_id in self._items:
            existing = self._items[media_id]
            if existing.path == str(path):
                return existing

        transcript_path, timestamps_path = self._find_transcript(path)

        # Read transcript if available
        full_transcript = ""
        word_count = 0
        if transcript_path:
            try:
                full_transcript = transcript_path.read_text(encoding="utf-8")
                # Strip markdown header if present
                if full_transcript.startswith("# Transcript"):
                    lines = full_transcript.split("\n", 2)
                    if len(lines) > 2:
                        full_transcript = lines[2].strip()
                word_count = len(full_transcript.split())
            except OSError:
                pass

        # Get recording date from file mtime
        try:
            mtime = path.stat().st_mtime
            recorded_at = datetime.fromtimestamp(mtime, tz=UTC).isoformat()
        except OSError:
            recorded_at = ""

        # Detect participants from transcript
        participants = self._detect_participants(full_transcript)

        item = MediaItem(
            media_id=media_id,
            path=str(path),
            media_type=media_type,
            title=path.stem.replace("_", " ").title(),
            transcript_path=str(transcript_path) if transcript_path else None,
            timestamps_path=str(timestamps_path) if timestamps_path else None,
            duration_sec=self._get_file_duration(path),
            recorded_at=recorded_at,
            word_count=word_count,
            transcript_preview=full_transcript[:200] + "..." if len(full_transcript) > 200 else full_transcript,
            full_transcript=full_transcript,
            participants=participants,
        )

        self._items[media_id] = item
        return item

    def rebuild_index(self, generate_embeddings: bool = True) -> int:
        """Rebuild the media index from scratch.

        Returns number of items indexed.
        """
        # Clear existing
        self._items.clear()
        self._embeddings.clear()

        # Index audio files
        if RAW_VOICE_DIR.exists():
            for ext in (".m4a", ".mp3", ".wav", ".webm", ".ogg", ".flac"):
                for path in RAW_VOICE_DIR.glob(f"*{ext}"):
                    self._index_media_file(path, MediaType.AUDIO)

        # Index video files
        if RAW_VIDEO_DIR.exists():
            for ext in (".mp4", ".mov", ".webm", ".mkv", ".avi"):
                for path in RAW_VIDEO_DIR.glob(f"*{ext}"):
                    self._index_media_file(path, MediaType.VIDEO)

        # Generate embeddings
        if generate_embeddings:
            self._generate_embeddings()

        self._save_index()
        return len(self._items)

    def update_index(self, generate_embeddings: bool = True) -> int:
        """Update index with new files only.

        Returns number of new items added.
        """
        initial_count = len(self._items)

        # Index new audio files
        if RAW_VOICE_DIR.exists():
            for ext in (".m4a", ".mp3", ".wav", ".webm", ".ogg", ".flac"):
                for path in RAW_VOICE_DIR.glob(f"*{ext}"):
                    self._index_media_file(path, MediaType.AUDIO)

        # Index new video files
        if RAW_VIDEO_DIR.exists():
            for ext in (".mp4", ".mov", ".webm", ".mkv", ".avi"):
                for path in RAW_VIDEO_DIR.glob(f"*{ext}"):
                    self._index_media_file(path, MediaType.VIDEO)

        new_count = len(self._items) - initial_count

        # Generate embeddings for new items
        if generate_embeddings and new_count > 0:
            self._generate_embeddings()

        self._save_index()
        return new_count

    def _generate_embeddings(self, batch_size: int = 20) -> None:
        """Generate embeddings for items without them."""
        embedder = self._get_embedder()

        # Find items needing embeddings
        need_embedding = [
            item for item in self._items.values()
            if item.media_id not in self._embeddings
            and item.full_transcript
        ]

        if not need_embedding:
            return

        # Batch embed
        for i in range(0, len(need_embedding), batch_size):
            batch = need_embedding[i:i + batch_size]
            texts = [item.full_transcript[:2000] for item in batch]  # Limit text length

            try:
                embeddings = embedder.embed(texts)
                for item, emb in zip(batch, embeddings):
                    self._embeddings[item.media_id] = emb
            except Exception as e:
                print(f"Warning: embedding failed: {e}")

    def _detect_search_mode(self, query: str) -> SearchMode:
        """Auto-detect optimal search mode based on query characteristics.

        Returns KEYWORD for:
        - Short queries (1-2 words)
        - Queries with quotes
        - Queries with technical terms/proper nouns
        - Queries with dates/numbers

        Returns SEMANTIC for:
        - Question-form queries
        - Longer descriptive queries
        - Queries about concepts/topics
        """
        query_lower = query.lower().strip()
        words = query_lower.split()

        # Quoted phrases -> keyword
        if '"' in query or "'" in query:
            return SearchMode.KEYWORD

        # Very short -> keyword
        if len(words) <= 2:
            return SearchMode.KEYWORD

        # Contains dates/times -> keyword
        if re.search(r'\b\d{1,2}[/-]\d{1,2}\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b|\b\d{4}\b', query_lower):
            return SearchMode.KEYWORD

        # Question words -> semantic
        question_starters = ["what", "when", "where", "who", "why", "how", "which", "can", "could", "would", "should", "is", "are", "was", "were", "do", "does", "did"]
        if any(query_lower.startswith(q) for q in question_starters):
            return SearchMode.SEMANTIC

        # Topic/concept words -> semantic
        concept_indicators = ["about", "regarding", "concerning", "related to", "discussion", "conversation", "talk", "mention", "said"]
        if any(ind in query_lower for ind in concept_indicators):
            return SearchMode.SEMANTIC

        # Longer queries tend to be semantic
        if len(words) >= 5:
            return SearchMode.SEMANTIC

        # Technical terms with underscores/camelCase -> keyword
        if "_" in query or any(c.isupper() for c in query[1:]):
            return SearchMode.KEYWORD

        # Default to hybrid for middle-ground queries
        return SearchMode.HYBRID

    def _keyword_search(
        self,
        query: str,
        items: list[MediaItem],
        top_k: int = 10,
    ) -> list[SearchResult]:
        """Keyword-based search using fuzzy matching."""
        query_lower = query.lower()
        query_words = set(query_lower.split())

        results = []

        for item in items:
            if not item.full_transcript:
                continue

            transcript_lower = item.full_transcript.lower()

            # Exact phrase match (highest score)
            if query_lower in transcript_lower:
                # Find position for segment extraction
                pos = transcript_lower.find(query_lower)
                context_start = max(0, pos - 100)
                context_end = min(len(item.full_transcript), pos + len(query) + 100)
                matched_text = item.full_transcript[context_start:context_end]

                results.append(SearchResult(
                    item=item,
                    score=1.0,
                    match_type=SearchMode.KEYWORD,
                    matched_text=matched_text,
                ))
                continue

            # Word overlap score
            transcript_words = set(transcript_lower.split())
            overlap = query_words & transcript_words
            if overlap:
                score = len(overlap) / len(query_words)

                # Find best matching segment
                best_segment = ""
                best_density = 0
                words_list = item.full_transcript.split()
                window_size = 50

                for i in range(0, len(words_list), 25):
                    window = " ".join(words_list[i:i + window_size])
                    window_lower = window.lower()
                    density = sum(1 for w in query_words if w in window_lower) / len(query_words)
                    if density > best_density:
                        best_density = density
                        best_segment = window

                results.append(SearchResult(
                    item=item,
                    score=score * 0.8,  # Discount vs exact match
                    match_type=SearchMode.KEYWORD,
                    matched_text=best_segment,
                ))

        results.sort(key=lambda r: -r.score)
        return results[:top_k]

    def _semantic_search(
        self,
        query: str,
        items: list[MediaItem],
        top_k: int = 10,
        min_score: float = 0.3,
    ) -> list[SearchResult]:
        """Semantic search using embeddings."""
        embedder = self._get_embedder()

        try:
            query_embedding = embedder.embed_single(query)
        except Exception:
            # Fallback to keyword
            return self._keyword_search(query, items, top_k)

        results = []

        for item in items:
            if item.media_id not in self._embeddings:
                continue

            item_embedding = self._embeddings[item.media_id]
            score = self._cosine_similarity(query_embedding, item_embedding)

            if score >= min_score:
                results.append(SearchResult(
                    item=item,
                    score=score,
                    match_type=SearchMode.SEMANTIC,
                    matched_text=item.transcript_preview,
                ))

        results.sort(key=lambda r: -r.score)
        return results[:top_k]

    def _cosine_similarity(self, a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors."""
        if len(a) != len(b):
            max_len = max(len(a), len(b))
            a = a + [0.0] * (max_len - len(a))
            b = b + [0.0] * (max_len - len(b))

        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot / (norm_a * norm_b)

    def search(
        self,
        query: str,
        mode: SearchMode = SearchMode.AUTO,
        top_k: int = 10,
        media_type: MediaType | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        accessible_to: str | None = None,
    ) -> list[SearchResult]:
        """Search the media library.

        Args:
            query: Search query
            mode: Search mode (AUTO detects best mode based on query)
            top_k: Max results to return
            media_type: Filter by media type
            date_from: Filter recordings after this date
            date_to: Filter recordings before this date
            accessible_to: Only return items this person has access to

        Returns:
            List of SearchResult objects
        """
        # Filter items
        items = list(self._items.values())

        # Access control filter
        if accessible_to:
            items = [i for i in items if i.has_access(accessible_to)]

        if media_type:
            items = [i for i in items if i.media_type == media_type]

        if date_from:
            items = [i for i in items if i.recorded_at and i.recorded_at >= date_from.isoformat()]

        if date_to:
            items = [i for i in items if i.recorded_at and i.recorded_at <= date_to.isoformat()]

        if not items:
            return []

        # Detect mode if auto
        if mode == SearchMode.AUTO:
            mode = self._detect_search_mode(query)

        # Execute search
        if mode == SearchMode.KEYWORD:
            return self._keyword_search(query, items, top_k)
        elif mode == SearchMode.SEMANTIC:
            return self._semantic_search(query, items, top_k)
        else:  # HYBRID
            # Combine keyword and semantic results
            keyword_results = self._keyword_search(query, items, top_k)
            semantic_results = self._semantic_search(query, items, top_k)

            # Merge with score combination
            seen_ids = set()
            merged = []

            # Add keyword results (higher weight for exact matches)
            for r in keyword_results:
                merged.append(SearchResult(
                    item=r.item,
                    score=r.score * 1.2,  # Boost keyword matches
                    match_type=SearchMode.HYBRID,
                    matched_text=r.matched_text,
                ))
                seen_ids.add(r.item.media_id)

            # Add semantic results not in keyword
            for r in semantic_results:
                if r.item.media_id not in seen_ids:
                    merged.append(r)
                else:
                    # Boost items found by both
                    for m in merged:
                        if m.item.media_id == r.item.media_id:
                            m.score += r.score * 0.5
                            break

            merged.sort(key=lambda r: -r.score)
            return merged[:top_k]

    def find_segment(
        self,
        query: str,
        media_id: str,
        duration_sec: float = 10.0,
    ) -> tuple[float, float, str] | None:
        """Find a specific segment within a recording.

        Returns (start_sec, end_sec, matched_text) or None.
        """
        item = self._items.get(media_id)
        if not item or not item.timestamps_path:
            return None

        timestamps_path = Path(item.timestamps_path)
        if not timestamps_path.exists():
            return None

        try:
            data = json.loads(timestamps_path.read_text(encoding="utf-8"))
            words = data.get("words", [])
        except (json.JSONDecodeError, OSError):
            return None

        if not words:
            return None

        # Fuzzy match query in words
        query_lower = query.lower().strip()
        query_words = query_lower.split()

        best_score = 0.0
        best_start_idx = 0
        best_end_idx = 0

        window_size = len(query_words)
        for i in range(len(words) - window_size + 1):
            window_text = " ".join(
                w.get("word", "").lower().strip(".,!?;:\"'")
                for w in words[i:i + window_size]
            )
            score = SequenceMatcher(None, query_lower, window_text).ratio()
            if score > best_score:
                best_score = score
                best_start_idx = i
                best_end_idx = i + window_size - 1

        if best_score < 0.4:
            return None

        # Get timing centered on match
        match_start = words[best_start_idx].get("start", 0)
        match_end = words[best_end_idx].get("end", match_start + 2)
        match_center = (match_start + match_end) / 2

        half_duration = duration_sec / 2
        start_sec = max(0, match_center - half_duration)
        end_sec = match_center + half_duration

        matched_text = " ".join(
            w.get("word", "") for w in words[best_start_idx:best_end_idx + 1]
        )

        return (start_sec, end_sec, matched_text)

    def play(
        self,
        media_id: str,
        start_sec: float | None = None,
        duration_sec: float | None = None,
    ) -> bool:
        """Play a recording or segment.

        Returns True if playback started successfully.
        """
        item = self._items.get(media_id)
        if not item:
            print(f"Media not found: {media_id}")
            return False

        path = Path(item.path)
        if not path.exists():
            print(f"File not found: {path}")
            return False

        # Use ffplay for segment playback
        if start_sec is not None:
            if shutil.which("ffplay"):
                cmd = [
                    "ffplay",
                    "-autoexit",
                    "-nodisp" if item.media_type == MediaType.AUDIO else "",
                    "-ss", str(start_sec),
                ]
                if duration_sec:
                    cmd.extend(["-t", str(duration_sec)])
                cmd.append(str(path))
                cmd = [c for c in cmd if c]  # Remove empty strings

                try:
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return True
                except OSError:
                    pass

        # Fallback: open with default player
        import platform
        system = platform.system()

        try:
            if system == "Darwin":
                subprocess.Popen(["open", str(path)])
            elif system == "Linux":
                subprocess.Popen(["xdg-open", str(path)])
            elif system == "Windows":
                os.startfile(str(path))  # type: ignore
            return True
        except OSError as e:
            print(f"Failed to open: {e}")
            return False

    def get_item(self, media_id: str) -> MediaItem | None:
        """Get a media item by ID."""
        return self._items.get(media_id)

    def list_items(
        self,
        media_type: MediaType | None = None,
        limit: int = 50,
        sort_by: Literal["date", "duration", "title"] = "date",
    ) -> list[MediaItem]:
        """List items in the library."""
        items = list(self._items.values())

        if media_type:
            items = [i for i in items if i.media_type == media_type]

        if sort_by == "date":
            items.sort(key=lambda i: i.recorded_at or "", reverse=True)
        elif sort_by == "duration":
            items.sort(key=lambda i: i.duration_sec, reverse=True)
        else:
            items.sort(key=lambda i: i.title.lower())

        return items[:limit]

    def stats(self) -> dict:
        """Get library statistics."""
        total_duration = sum(i.duration_sec for i in self._items.values())
        total_words = sum(i.word_count for i in self._items.values())

        audio_count = sum(1 for i in self._items.values() if i.media_type == MediaType.AUDIO)
        video_count = sum(1 for i in self._items.values() if i.media_type == MediaType.VIDEO)
        with_transcript = sum(1 for i in self._items.values() if i.transcript_path)
        with_embedding = len(self._embeddings)

        # Count unique participants
        all_participants = set()
        for item in self._items.values():
            for p in item.participants:
                all_participants.add(p.person_id)

        return {
            "total_items": len(self._items),
            "audio_count": audio_count,
            "video_count": video_count,
            "total_duration_sec": total_duration,
            "total_duration_hours": total_duration / 3600,
            "total_words": total_words,
            "with_transcript": with_transcript,
            "with_embedding": with_embedding,
            "unique_participants": len(all_participants),
        }

    # =========================================================================
    # Participant-Based Access
    # =========================================================================

    def find_recordings_with(
        self,
        person_id: str,
        role: str | None = None,
        limit: int = 50,
    ) -> list[MediaItem]:
        """Find all recordings where a person appears.

        Args:
            person_id: Person identifier
            role: Filter by role ("speaker", "mentioned", "shared")
            limit: Max results

        Returns:
            List of MediaItem objects, sorted by recency
        """
        results = []

        for item in self._items.values():
            for p in item.participants:
                if p.person_id == person_id:
                    if role is None or p.role == role:
                        results.append(item)
                        break

        results.sort(key=lambda i: i.recorded_at or "", reverse=True)
        return results[:limit]

    def accessible_to(self, person_id: str, limit: int = 50) -> list[MediaItem]:
        """List all recordings a person can access.

        This includes:
        - Recordings they own
        - Recordings where they are a participant
        - Recordings explicitly shared with them
        """
        results = [
            item for item in self._items.values()
            if item.has_access(person_id)
        ]
        results.sort(key=lambda i: i.recorded_at or "", reverse=True)
        return results[:limit]

    def set_owner(self, media_id: str, owner_id: str) -> bool:
        """Set the owner of a recording."""
        item = self._items.get(media_id)
        if not item:
            return False
        item.owner_id = owner_id
        self._save_index()
        return True

    def share_with(
        self,
        media_id: str,
        person_id: str,
        person_name: str,
        requester_id: str,
    ) -> bool:
        """Share a recording with another person.

        Only the owner can share.
        """
        item = self._items.get(media_id)
        if not item:
            return False

        # Check requester is owner
        if item.owner_id and item.owner_id != requester_id:
            return False

        item.grant_access(person_id, person_name, AccessLevel.SHARED)
        self._save_index()
        return True

    def list_participants_summary(self) -> list[dict]:
        """Get summary of all detected participants across recordings.

        Useful for "who have I recorded?"
        """
        participant_stats: dict[str, dict] = {}

        for item in self._items.values():
            for p in item.participants:
                if p.person_id not in participant_stats:
                    participant_stats[p.person_id] = {
                        "person_id": p.person_id,
                        "name": p.name,
                        "recording_count": 0,
                        "total_mentions": 0,
                        "roles": set(),
                    }
                stats = participant_stats[p.person_id]
                stats["recording_count"] += 1
                stats["total_mentions"] += p.mention_count
                stats["roles"].add(p.role)

        # Convert to list and sort
        results = []
        for stats in participant_stats.values():
            stats["roles"] = list(stats["roles"])
            results.append(stats)

        results.sort(key=lambda x: -x["recording_count"])
        return results

    def enrich_rag(self) -> int:
        """Add media transcripts to the signal RAG for unified search.

        Returns number of items added to RAG.
        """
        from .signal_rag import SignalRAG

        rag = SignalRAG()

        # Convert media items to signals for RAG
        signals = []
        for item in self._items.values():
            if not item.full_transcript:
                continue

            signals.append({
                "signal_id": f"media_{item.media_id}",
                "detail": f"Recording: {item.title}",
                "raw_text": item.full_transcript[:1500],
                "summary": item.transcript_preview,
                "signal_type": "recording",
                "initiative": "",
                "source": item.path,
                "timestamp": item.recorded_at,
            })

        if signals:
            return rag.index_signals(signals)
        return 0

    def discuss(self, result: SearchResult) -> NeutExplainer:
        """Start an interactive discussion with Neut about a search result.

        Returns a NeutExplainer that can explain content, add context,
        and answer questions about the recording.

        Usage:
            results = library.search("heat exchangers")
            neut = library.discuss(results[0])
            neut.explain("What was the main decision here?")
            neut.concepts()  # Explain technical terms
            neut.summarize()  # Quick summary
        """
        return NeutExplainer(result)


# ============================================================================
# Neut Explainer — Interactive AI assistant for media content
# ============================================================================

@dataclass
class NeutMessage:
    """A message in a Neut conversation."""
    role: Literal["user", "assistant", "system"]
    content: str
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(UTC).isoformat()


class NeutExplainer:
    """Interactive AI assistant that explains recording content.

    Neut can:
    - Explain what was said/discussed in a recording
    - Add color and context (who the speakers are, background)
    - Explain technical concepts mentioned
    - Answer questions about the content
    - Summarize key points, decisions, and action items

    Usage:
        neut = NeutExplainer(search_result)

        # Quick actions
        print(neut.summarize())
        print(neut.concepts())

        # Interactive Q&A
        print(neut.ask("What was the decision about the pump?"))
        print(neut.ask("Why is that important?"))  # Follows conversation
    """

    def __init__(self, result: SearchResult):
        self.result = result
        self.item = result.item
        self._gateway = None
        self._history: list[NeutMessage] = []
        self._system_prompt = self._build_system_prompt()

    def _get_gateway(self):
        """Lazy load gateway."""
        if self._gateway is None:
            from axiom.infra.gateway import Gateway
            self._gateway = Gateway()
        return self._gateway

    def _build_system_prompt(self) -> str:
        """Build context-rich system prompt for Neut."""
        item = self.item
        result = self.result

        # Basic recording info
        info_parts = [
            f"Title: {item.title}" if item.title else None,
            f"Type: {item.media_type.value}",
            f"Recorded: {item.recorded_at}" if item.recorded_at else None,
            f"Duration: {format_duration(item.duration_sec)}" if item.duration_sec else None,
            f"Search match: {result.matched_text[:200]}..." if result.matched_text else None,
        ]
        recording_info = "\n".join(p for p in info_parts if p)

        # Participants context
        participant_context = ""
        if item.participants:
            people = ", ".join(f"{p.name} ({p.role})" for p in item.participants)
            participant_context = f"\n\nPeople in this recording: {people}"

        # Truncate transcript if too long (keep first/last + matched section)
        transcript = item.full_transcript or item.transcript_preview or ""
        if len(transcript) > 8000:
            # Smart truncation: keep start, end, and matched section
            start = transcript[:2000]
            end = transcript[-2000:]

            # Find matched section if available
            matched_section = ""
            if result.matched_text and result.matched_text in transcript:
                match_pos = transcript.find(result.matched_text)
                match_start = max(0, match_pos - 500)
                match_end = min(len(transcript), match_pos + len(result.matched_text) + 500)
                matched_section = f"\n\n[...matched section...]\n{transcript[match_start:match_end]}\n[...]"

            transcript = f"{start}\n\n[...truncated...]{matched_section}\n\n[...end of recording...]\n{end}"

        system_prompt = f"""You are Neut, an AI assistant helping explain and discuss recorded content.

You have access to a recording with the following information:

## Recording Details
{recording_info}{participant_context}

## Full Transcript
{transcript}

## Your Role
- Explain what's being discussed in the recording
- Add helpful context about people, projects, or concepts mentioned
- Answer questions about the content accurately based on what was said
- Highlight key decisions, action items, or important points
- Explain technical concepts in accessible terms
- Point out connections to other projects or discussions if relevant

Be conversational and helpful. When explaining concepts, provide just enough detail to be useful without being overwhelming. If something isn't clear from the transcript, say so rather than guessing."""

        return system_prompt

    def _complete(self, prompt: str, include_history: bool = True) -> str:
        """Send a completion request with conversation history."""
        gateway = self._get_gateway()

        if not gateway.available:
            return "(Neut unavailable - no LLM configured. Set OPENAI_API_KEY or configure models.toml)"

        # Build conversation context
        if include_history and self._history:
            history_text = "\n\n".join(
                f"{msg.role.upper()}: {msg.content}"
                for msg in self._history[-6:]  # Keep last 3 exchanges
            )
            full_prompt = f"Previous conversation:\n{history_text}\n\nUSER: {prompt}"
        else:
            full_prompt = prompt

        response = gateway.complete(
            prompt=full_prompt,
            system=self._system_prompt,
            task="briefing",
            max_tokens=1500,
        )

        if not response.success:
            return f"(Error: {response.error})"

        # Track history
        self._history.append(NeutMessage(role="user", content=prompt))
        self._history.append(NeutMessage(role="assistant", content=response.text))

        return response.text

    def ask(self, question: str) -> str:
        """Ask Neut any question about the recording.

        Maintains conversation context for follow-up questions.
        """
        return self._complete(question)

    def summarize(self) -> str:
        """Get a concise summary of the recording."""
        return self._complete(
            "Provide a concise summary of this recording in 3-5 bullet points. "
            "Focus on key topics, decisions, and action items.",
            include_history=False,
        )

    def concepts(self) -> str:
        """Explain technical concepts mentioned in the recording."""
        return self._complete(
            "Identify and briefly explain any technical concepts, acronyms, or "
            "domain-specific terms mentioned in this recording. Keep explanations "
            "accessible but accurate.",
            include_history=False,
        )

    def context(self) -> str:
        """Provide background context for the discussion."""
        return self._complete(
            "Based on the recording, provide helpful background context. "
            "Who are the people involved? What project or initiative is this about? "
            "What's the broader situation or history behind this discussion?",
            include_history=False,
        )

    def action_items(self) -> str:
        """Extract action items and decisions from the recording."""
        return self._complete(
            "Extract any action items, decisions, or commitments from this recording. "
            "For each, note who is responsible and any deadlines mentioned.",
            include_history=False,
        )

    def explain_segment(self, text: str) -> str:
        """Explain a specific segment or quote from the recording."""
        return self._complete(
            f"Explain this specific part of the recording: \"{text}\"\n\n"
            "What does it mean? What's the context? Why is it significant?",
        )

    def interactive(self) -> None:
        """Start an interactive REPL session with Neut.

        Type questions to discuss the recording.
        Special commands: /summary, /concepts, /context, /actions, /quit
        """
        print(f"\n🦎 Neut is ready to discuss: {self.item.title or 'this recording'}")
        print("Type your questions. Commands: /summary /concepts /context /actions /quit\n")

        while True:
            try:
                user_input = input("You: ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\n👋 Bye!")
                break

            if not user_input:
                continue

            # Handle commands
            if user_input.lower() == "/quit":
                print("👋 Bye!")
                break
            elif user_input.lower() == "/summary":
                print(f"\nNeut: {self.summarize()}\n")
            elif user_input.lower() == "/concepts":
                print(f"\nNeut: {self.concepts()}\n")
            elif user_input.lower() == "/context":
                print(f"\nNeut: {self.context()}\n")
            elif user_input.lower() == "/actions":
                print(f"\nNeut: {self.action_items()}\n")
            elif user_input.startswith("/"):
                print("Commands: /summary /concepts /context /actions /quit")
            else:
                response = self.ask(user_input)
                print(f"\nNeut: {response}\n")

    def clear_history(self) -> None:
        """Clear conversation history for fresh context."""
        self._history.clear()


def format_duration(seconds: float) -> str:
    """Format duration as HH:MM:SS or MM:SS."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
