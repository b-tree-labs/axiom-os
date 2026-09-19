# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Signal RAG — Retrieval Augmented Generation for signal queries.

Provides semantic search over accumulated signals to support:
- Topic-focused briefings ("brief me on Kevin")
- Pattern detection ("what's the status of the Alpha initiative?")
- Historical context ("what did we discuss about thermal systems last month?")

Architecture:
- Embeddings generated via OpenAI or local model
- Vector store: simple FAISS or in-memory for small scale
- Chunking: signals are already atomic, but we may group by session

Usage:
    from axiom.extensions.builtins.signals.signal_rag import SignalRAG

    rag = SignalRAG()
    rag.index_signals(signals)

    results = rag.query("What's the status of the Alpha initiative?")
    for r in results:
        print(r.signal.detail, r.relevance)
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.state import atomic_write
from axiom.infra.time_utils import parse_iso

_RUNTIME_DIR = _REPO_ROOT / "runtime"
INDEX_PATH = _RUNTIME_DIR / "inbox" / "cache" / "signal_index.json"
EMBEDDINGS_PATH = _RUNTIME_DIR / "inbox" / "cache" / "signal_embeddings.json"
PROCESSED_DIR = _RUNTIME_DIR / "inbox" / "processed"


@dataclass
class SignalChunk:
    """A signal prepared for RAG indexing."""

    signal_id: str
    text: str  # Combined text for embedding
    timestamp: str
    signal_type: str
    initiative: str
    source: str
    metadata: dict = field(default_factory=dict)
    embedding: list[float] | None = None

    def to_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "text": self.text,
            "timestamp": self.timestamp,
            "signal_type": self.signal_type,
            "initiative": self.initiative,
            "source": self.source,
            "metadata": self.metadata,
            # Don't persist embeddings here - separate file
        }

    @classmethod
    def from_dict(cls, data: dict) -> SignalChunk:
        return cls(
            signal_id=data["signal_id"],
            text=data["text"],
            timestamp=data.get("timestamp", ""),
            signal_type=data.get("signal_type", ""),
            initiative=data.get("initiative", ""),
            source=data.get("source", ""),
            metadata=data.get("metadata", {}),
        )


@dataclass
class RetrievalResult:
    """A signal retrieved by RAG query."""

    chunk: SignalChunk
    relevance: float  # 0-1, higher is more relevant
    match_reason: str = ""  # Why this matched


# ============================================================================
# Topic Categories
# ============================================================================

class TopicCategory:
    """Built-in topic categories for focused briefings."""

    PEOPLE = "people"
    INITIATIVES = "initiatives"
    TECH = "tech"
    ROADMAPS = "roadmaps"
    CONFERENCES = "conferences"
    TRAVEL = "travel"
    BLOCKERS = "blockers"
    DECISIONS = "decisions"
    MILESTONES = "milestones"

    # Auto-detected long-running topics
    LONG_RUNNING = "long_running"


# Topic keywords for basic matching (used when embeddings unavailable)
TOPIC_KEYWORDS = {
    TopicCategory.PEOPLE: [
        "kevin", "andres", "ben", "meeting with", "talked to", "spoke with",
        "said", "mentioned", "asked", "told", "email from", "call with",
        "1:1", "one on one", "sync with",
    ],
    TopicCategory.TECH: [
        "code", "bug", "feature", "api", "database", "server", "deploy",
        "git", "merge", "branch", "test", "ci", "cd", "docker", "kubernetes",
        "python", "javascript", "react", "model", "simulation", "thermal",
        "hydraulics", "simulation", "analysis", "optimization",
    ],
    TopicCategory.INITIATIVES: [
        "initiative", "project", "prd", "roadmap", "milestone", "deliverable",
        "phase", "sprint", "quarter", "goal", "objective", "okr",
        "digital twin", "data pipeline", "platform", "integration",
    ],
    TopicCategory.ROADMAPS: [
        "roadmap", "timeline", "schedule", "deadline", "due date", "milestone",
        "q1", "q2", "q3", "q4", "2025", "2026", "next month", "next quarter",
        "planning", "backlog", "priority",
    ],
    TopicCategory.CONFERENCES: [
        "conference", "symposium", "workshop", "presentation", "poster",
        "abstract", "paper", "submission", "ans", "nureth", "physor",
        "talk", "keynote", "panel",
    ],
    TopicCategory.TRAVEL: [
        "travel", "trip", "flight", "hotel", "conference", "visit",
        "onsite", "remote", "meeting", "austin", "chicago", "dc",
        "washington", "san francisco", "airport",
    ],
    TopicCategory.BLOCKERS: [
        "blocked", "blocker", "waiting", "stuck", "issue", "problem",
        "can't", "unable", "dependency", "need", "require", "missing",
    ],
    TopicCategory.DECISIONS: [
        "decided", "decision", "approved", "rejected", "chose", "selected",
        "go with", "agreed", "consensus", "vote", "sign off",
    ],
    TopicCategory.MILESTONES: [
        "milestone", "completed", "finished", "shipped", "launched",
        "released", "deployed", "delivered", "achieved", "reached",
    ],
}


@dataclass
class DetectedTopic:
    """A topic detected from signal patterns."""

    topic_id: str
    name: str
    category: str
    keywords: list[str]
    signal_count: int
    first_seen: str
    last_seen: str
    is_long_running: bool = False

    def to_dict(self) -> dict:
        return {
            "topic_id": self.topic_id,
            "name": self.name,
            "category": self.category,
            "keywords": self.keywords,
            "signal_count": self.signal_count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "is_long_running": self.is_long_running,
        }


# ============================================================================
# Embedding Providers
# ============================================================================

class EmbeddingProvider:
    """Base class for embedding providers."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_single(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def is_available(self) -> bool:
        return False


class OpenAIEmbeddings(EmbeddingProvider):
    """OpenAI text-embedding-3-small embeddings."""

    def __init__(self, model: str = "text-embedding-3-small"):
        self.model = model
        from axiom.infra.connections import get_credential
        self.api_key = get_credential("openai") or os.environ.get("OPENAI_API_KEY")

    def is_available(self) -> bool:
        return bool(self.api_key)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.is_available():
            raise RuntimeError("OpenAI API key not configured")

        import openai  # type: ignore[import-untyped]
        client = openai.OpenAI(api_key=self.api_key)

        response = client.embeddings.create(
            model=self.model,
            input=texts,
        )

        return [item.embedding for item in response.data]


class LocalEmbeddings(EmbeddingProvider):
    """Local embeddings using sentence-transformers (if installed)."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self.model_name = model_name
        self._model = None

    def is_available(self) -> bool:
        try:
            from sentence_transformers import (
                SentenceTransformer,  # type: ignore[import-untyped]  # noqa: F401
            )
            return True
        except ImportError:
            return False

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-untyped]
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._get_model()
        embeddings = model.encode(texts)
        return [emb.tolist() for emb in embeddings]


class KeywordEmbeddings(EmbeddingProvider):
    """Fallback: keyword-based pseudo-embeddings (TF-IDF style)."""

    def __init__(self):
        self.vocabulary: dict[str, int] = {}
        self.idf: dict[str, float] = {}

    def is_available(self) -> bool:
        return True  # Always available

    def _tokenize(self, text: str) -> list[str]:
        import re
        # Simple tokenization
        text = text.lower()
        tokens = re.findall(r'\b\w+\b', text)
        return [t for t in tokens if len(t) > 2]

    def embed(self, texts: list[str]) -> list[list[float]]:
        # Build vocabulary from all texts
        all_tokens = set()
        doc_tokens = []
        for text in texts:
            tokens = self._tokenize(text)
            doc_tokens.append(tokens)
            all_tokens.update(tokens)

        # Create fixed-size vocab (top 500 terms by frequency)
        from collections import Counter
        freq = Counter(t for tokens in doc_tokens for t in tokens)
        top_terms = [t for t, _ in freq.most_common(500)]
        vocab = {t: i for i, t in enumerate(top_terms)}

        # Generate embeddings
        embeddings = []
        for tokens in doc_tokens:
            vec = [0.0] * len(vocab)
            token_freq = Counter(tokens)
            for token, count in token_freq.items():
                if token in vocab:
                    vec[vocab[token]] = count / len(tokens)  # Normalized TF
            embeddings.append(vec)

        return embeddings


# ============================================================================
# Vector Store
# ============================================================================

class VectorStore:
    """Simple in-memory vector store with cosine similarity."""

    def __init__(self):
        self.chunks: list[SignalChunk] = []
        self.embeddings: list[list[float]] = []

    def add(self, chunk: SignalChunk, embedding: list[float]) -> None:
        self.chunks.append(chunk)
        self.embeddings.append(embedding)

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        min_score: float = 0.0,
    ) -> list[tuple[SignalChunk, float]]:
        """Search for similar chunks."""
        if not self.embeddings:
            return []

        scores = []
        for i, emb in enumerate(self.embeddings):
            score = self._cosine_similarity(query_embedding, emb)
            if score >= min_score:
                scores.append((i, score))

        # Sort by score descending
        scores.sort(key=lambda x: -x[1])

        results = []
        for idx, score in scores[:top_k]:
            results.append((self.chunks[idx], score))

        return results

    def _cosine_similarity(self, a: list[float], b: list[float]) -> float:
        if len(a) != len(b):
            # Pad shorter vector
            max_len = max(len(a), len(b))
            a = a + [0.0] * (max_len - len(a))
            b = b + [0.0] * (max_len - len(b))

        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot / (norm_a * norm_b)

    def save(self, index_path: Path, embeddings_path: Path) -> None:
        """Persist to disk."""
        index_path.parent.mkdir(parents=True, exist_ok=True)

        index_data = [c.to_dict() for c in self.chunks]
        atomic_write(index_path, index_data)
        atomic_write(embeddings_path, self.embeddings)

    def load(self, index_path: Path, embeddings_path: Path) -> bool:
        """Load from disk. Returns True if successful."""
        if not index_path.exists() or not embeddings_path.exists():
            return False

        try:
            index_data = json.loads(index_path.read_text())
            self.chunks = [SignalChunk.from_dict(d) for d in index_data]
            self.embeddings = json.loads(embeddings_path.read_text())
            return True
        except (json.JSONDecodeError, KeyError):
            return False


# ============================================================================
# Signal RAG
# ============================================================================

class SignalRAG:
    """RAG system for signal retrieval.

    Uses PostgreSQL + pgvector when AXIOM_DB_URL is configured,
    otherwise falls back to file-based VectorStore.
    """

    def __init__(
        self,
        index_path: Path | None = None,
        embeddings_path: Path | None = None,
        use_pgvector: bool | None = None,
    ):
        self.index_path = index_path or INDEX_PATH
        self.embeddings_path = embeddings_path or EMBEDDINGS_PATH

        # Initialize embedding provider
        self.embedder = self._get_embedder()

        # Determine backend: pgvector if configured, else file-based
        if use_pgvector is None:
            use_pgvector = bool(os.environ.get("AXIOM_DB_URL"))

        if use_pgvector:
            try:
                from .pgvector_store import PgVectorStore
                self.store = PgVectorStore()
                self._using_pgvector = True
            except Exception as e:
                print(f"Warning: pgvector unavailable ({e}), using file store")
                self.store = VectorStore()
                self.store.load(self.index_path, self.embeddings_path)
                self._using_pgvector = False
        else:
            self.store = VectorStore()
            self.store.load(self.index_path, self.embeddings_path)
            self._using_pgvector = False

        # Topic tracking
        self.detected_topics: dict[str, DetectedTopic] = {}

    @property
    def backend_name(self) -> str:
        """Return name of active backend."""
        return "pgvector" if self._using_pgvector else "file"

    def _get_embedder(self) -> EmbeddingProvider:
        """Get best available embedding provider."""
        openai = OpenAIEmbeddings()
        if openai.is_available():
            return openai

        local = LocalEmbeddings()
        if local.is_available():
            return local

        # Fallback to keyword-based
        return KeywordEmbeddings()

    def _signal_to_chunk(self, signal: dict) -> SignalChunk:
        """Convert a signal dict to a chunk for indexing."""
        # Build text for embedding
        parts = []

        if signal.get("detail"):
            parts.append(signal["detail"])
        if signal.get("raw_text"):
            parts.append(signal["raw_text"][:500])
        if signal.get("summary"):
            parts.append(signal["summary"])
        if signal.get("initiative"):
            parts.append(f"Initiative: {signal['initiative']}")
        if signal.get("signal_type"):
            parts.append(f"Type: {signal['signal_type']}")

        text = " ".join(parts)

        # Generate ID
        signal_id = signal.get("signal_id") or hashlib.sha256(
            text.encode()
        ).hexdigest()[:16]

        return SignalChunk(
            signal_id=signal_id,
            text=text,
            timestamp=signal.get("timestamp", ""),
            signal_type=signal.get("signal_type", ""),
            initiative=signal.get("initiative", ""),
            source=signal.get("source", ""),
            metadata={
                "detail": signal.get("detail", ""),
                "confidence": signal.get("confidence", 0),
            },
        )

    def index_signals(self, signals: list[dict], batch_size: int = 50) -> int:
        """Index signals for RAG retrieval.

        Returns number of new signals indexed.
        """
        # Get existing IDs
        existing_ids = {c.signal_id for c in self.store.chunks}

        # Filter to new signals
        new_chunks = []
        for sig in signals:
            chunk = self._signal_to_chunk(sig)
            if chunk.signal_id not in existing_ids:
                new_chunks.append(chunk)

        if not new_chunks:
            return 0

        # Generate embeddings in batches
        for i in range(0, len(new_chunks), batch_size):
            batch = new_chunks[i:i + batch_size]
            texts = [c.text for c in batch]

            try:
                embeddings = self.embedder.embed(texts)
                for chunk, emb in zip(batch, embeddings):
                    self.store.add(chunk, emb)
            except Exception as e:
                print(f"Warning: embedding failed for batch: {e}")
                continue

        # Save updated index
        self.store.save(self.index_path, self.embeddings_path)

        return len(new_chunks)

    def query(
        self,
        query: str,
        top_k: int = 10,
        min_relevance: float = 0.3,
        time_filter: tuple[datetime, datetime] | None = None,
        category_filter: str | None = None,
    ) -> list[RetrievalResult]:
        """Query the signal index.

        Args:
            query: Natural language query
            top_k: Max results to return
            min_relevance: Minimum relevance score (0-1)
            time_filter: (start, end) datetime tuple
            category_filter: Filter to specific topic category

        Returns:
            List of RetrievalResult ordered by relevance
        """
        if not self.store.chunks:
            return []

        # Generate query embedding
        try:
            query_emb = self.embedder.embed_single(query)
        except Exception:
            # Fallback to keyword matching
            return self._keyword_search(query, top_k, category_filter)

        # Search
        results = self.store.search(query_emb, top_k * 2, min_relevance)

        # Apply filters
        filtered = []
        for chunk, score in results:
            # Time filter
            if time_filter and chunk.timestamp:
                try:
                    ts = parse_iso(chunk.timestamp)
                    if ts < time_filter[0] or ts > time_filter[1]:
                        continue
                except ValueError:
                    pass

            # Category filter
            if category_filter:
                if not self._matches_category(chunk, category_filter):
                    continue

            filtered.append(RetrievalResult(
                chunk=chunk,
                relevance=score,
                match_reason=f"Semantic similarity: {score:.2f}",
            ))

            if len(filtered) >= top_k:
                break

        return filtered

    def _keyword_search(
        self,
        query: str,
        top_k: int,
        category_filter: str | None,
    ) -> list[RetrievalResult]:
        """Fallback keyword-based search."""
        query_lower = query.lower()
        query_words = set(query_lower.split())

        scored = []
        for chunk in self.store.chunks:
            chunk_lower = chunk.text.lower()

            # Simple word overlap scoring
            chunk_words = set(chunk_lower.split())
            overlap = len(query_words & chunk_words)

            if overlap == 0:
                continue

            score = overlap / len(query_words)

            # Category filter
            if category_filter and not self._matches_category(chunk, category_filter):
                continue

            scored.append((chunk, score))

        scored.sort(key=lambda x: -x[1])

        return [
            RetrievalResult(chunk=c, relevance=s, match_reason="Keyword match")
            for c, s in scored[:top_k]
        ]

    def _matches_category(self, chunk: SignalChunk, category: str) -> bool:
        """Check if chunk matches a topic category."""
        keywords = TOPIC_KEYWORDS.get(category, [])
        if not keywords:
            return True

        text_lower = chunk.text.lower()
        return any(kw in text_lower for kw in keywords)

    def query_by_topic(
        self,
        topic: str,
        top_k: int = 10,
        time_window_days: int = 7,
    ) -> list[RetrievalResult]:
        """Query by topic category or detected topic."""
        # Check if it's a built-in category
        category = None
        for cat in [
            TopicCategory.PEOPLE, TopicCategory.TECH, TopicCategory.INITIATIVES,
            TopicCategory.ROADMAPS, TopicCategory.CONFERENCES, TopicCategory.TRAVEL,
            TopicCategory.BLOCKERS, TopicCategory.DECISIONS, TopicCategory.MILESTONES,
        ]:
            if topic.lower() in cat.lower() or cat.lower() in topic.lower():
                category = cat
                break

        # Build query
        if category:
            # Use category keywords as query
            keywords = TOPIC_KEYWORDS.get(category, [])
            query = " ".join(keywords[:10])
        else:
            # Treat as free-form topic query
            query = topic

        # Time filter
        now = datetime.now(UTC)
        start = now - timedelta(days=time_window_days)

        return self.query(
            query,
            top_k=top_k,
            time_filter=(start, now),
            category_filter=category,
        )

    def detect_long_running_topics(
        self,
        min_signals: int = 3,
        min_days: int = 7,
    ) -> list[DetectedTopic]:
        """Detect topics that appear consistently over time."""
        from collections import defaultdict

        # Group signals by potential topics (naive: by initiative + type)
        topic_signals: dict[str, list[SignalChunk]] = defaultdict(list)

        for chunk in self.store.chunks:
            # Key by initiative if present
            if chunk.initiative:
                key = f"initiative:{chunk.initiative}"
                topic_signals[key].append(chunk)

            # Also key by signal type
            if chunk.signal_type:
                key = f"type:{chunk.signal_type}"
                topic_signals[key].append(chunk)

        # Find long-running topics
        detected = []
        for key, chunks in topic_signals.items():
            if len(chunks) < min_signals:
                continue

            # Check time span
            timestamps = []
            for c in chunks:
                if c.timestamp:
                    try:
                        ts = parse_iso(c.timestamp)
                        timestamps.append(ts)
                    except ValueError:
                        pass

            if len(timestamps) < 2:
                continue

            first = min(timestamps)
            last = max(timestamps)
            span_days = (last - first).days

            if span_days < min_days:
                continue

            # Extract topic name
            category, name = key.split(":", 1)
            topic_id = hashlib.sha256(key.encode()).hexdigest()[:12]

            detected.append(DetectedTopic(
                topic_id=topic_id,
                name=name,
                category=category,
                keywords=[name.lower()],
                signal_count=len(chunks),
                first_seen=first.isoformat(),
                last_seen=last.isoformat(),
                is_long_running=True,
            ))

        # Sort by signal count
        detected.sort(key=lambda t: -t.signal_count)

        return detected

    def get_person_context(self, person_name: str, days: int = 30) -> list[RetrievalResult]:
        """Get all signals mentioning a person."""
        # Query with person name
        results = self.query(
            person_name,
            top_k=20,
            min_relevance=0.2,
            time_filter=(
                datetime.now(UTC) - timedelta(days=days),
                datetime.now(UTC),
            ),
        )

        # Also do direct text search
        person_lower = person_name.lower()
        for chunk in self.store.chunks:
            if person_lower in chunk.text.lower():
                # Check if already in results
                if not any(r.chunk.signal_id == chunk.signal_id for r in results):
                    results.append(RetrievalResult(
                        chunk=chunk,
                        relevance=0.8,
                        match_reason=f"Direct mention of '{person_name}'",
                    ))

        # Sort by relevance
        results.sort(key=lambda r: -r.relevance)
        return results[:20]

    def status(self) -> dict:
        """Get RAG system status."""
        return {
            "indexed_signals": len(self.store.chunks),
            "embedder": type(self.embedder).__name__,
            "embedder_available": self.embedder.is_available(),
            "index_path": str(self.index_path),
            "has_index": self.index_path.exists(),
        }


def get_signal_rag() -> SignalRAG:
    """Get singleton RAG instance."""
    return SignalRAG()


def reindex_all_signals() -> int:
    """Re-index all processed signals."""
    rag = SignalRAG()

    # Load all signals from processed
    all_signals = []
    for json_file in PROCESSED_DIR.glob("signals_*.json"):
        try:
            data = json.loads(json_file.read_text())
            all_signals.extend(data)
        except (json.JSONDecodeError, OSError):
            continue

    if not all_signals:
        return 0

    # Clear existing index
    rag.store = VectorStore()

    # Index all
    return rag.index_signals(all_signals)
