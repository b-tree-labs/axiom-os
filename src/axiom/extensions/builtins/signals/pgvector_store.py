# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PostgreSQL + pgvector backend for scalable RAG and media library.

Unified deployment: same PostgreSQL + pgvector stack everywhere.
- Local: K3D (Kubernetes in Docker)
- Staging: Kubernetes cluster
- Production: Kubernetes cluster

No SQLite fallback. Same stack, same config, predictable behavior.

Quick Start (local):
    # Start local cluster
    axi db up

    # Use it
    from axiom.extensions.builtins.signals.pgvector_store import VectorDB
    db = VectorDB()
    db.upsert_signal(signal, embedding)
    results = db.search_signals(query_embedding)

Environment:
    AXIOM_DB_URL=postgresql://axiom:axiom@localhost:5432/axiom_db  # local K3D
    AXIOM_DB_URL=postgresql://user:pass@db.staging.neut.dev:5432/neut  # staging
    AXIOM_DB_URL=postgresql://user:pass@db.neut.dev:5432/neut  # production
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom import REPO_ROOT as _REPO_ROOT

_RUNTIME_DIR = _REPO_ROOT / "runtime"
K3D_CONFIG_DIR = _REPO_ROOT / "infra" / "k3d"

# Default connection for local K3D cluster
DEFAULT_LOCAL_URL = "postgresql://axiom:axiom@localhost:5432/axiom_db"


# ============================================================================
# Data Models
# ============================================================================


@dataclass
class SignalRecord:
    """A signal chunk with embedding for vector search."""

    id: str
    text: str
    embedding: list[float]
    signal_type: str = ""
    initiative: str = ""
    source: str = ""
    timestamp: str = ""
    metadata: dict = field(default_factory=dict)

    # Audit fields
    created_at: str = ""
    updated_at: str = ""
    owner_id: str = ""
    version: int = 1

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "embedding": self.embedding,
            "signal_type": self.signal_type,
            "initiative": self.initiative,
            "source": self.source,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "owner_id": self.owner_id,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SignalRecord:
        return cls(
            id=data["id"],
            text=data["text"],
            embedding=data.get("embedding", []),
            signal_type=data.get("signal_type", ""),
            initiative=data.get("initiative", ""),
            source=data.get("source", ""),
            timestamp=data.get("timestamp", ""),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            owner_id=data.get("owner_id", ""),
            version=data.get("version", 1),
        )


@dataclass
class MediaRecord:
    """A media item (recording) with transcript and embedding."""

    id: str
    path: str
    media_type: str  # "audio" or "video"
    title: str = ""
    transcript: str = ""
    transcript_preview: str = ""
    embedding: list[float] = field(default_factory=list)
    duration_sec: float = 0.0
    recorded_at: str = ""

    # Access control
    owner_id: str = ""

    # Audit fields
    created_at: str = ""
    updated_at: str = ""
    version: int = 1


@dataclass
class ParticipantRecord:
    """A participant in a recording."""

    id: str
    media_id: str
    person_id: str
    name: str
    role: str = ""  # "speaker", "mentioned", "attendee"
    access_level: str = "participant"  # "owner", "participant", "shared", "none"
    mention_count: int = 0
    created_at: str = ""


@dataclass
class SearchResult:
    """A vector search result."""

    record: SignalRecord | MediaRecord
    score: float
    distance: float = 0.0


# ============================================================================
# PostgreSQL + pgvector Backend
# ============================================================================


class VectorDB:
    """PostgreSQL + pgvector vector database.

    Same stack for local (K3D), staging, and production.

    Requires:
        pip install psycopg2-binary  # or psycopg[binary]
        PostgreSQL 15+ with pgvector extension

    Environment:
        AXIOM_DB_URL=postgresql://user:pass@host:5432/axiom_db

    For local development, use K3D:
        axi db up    # Start local PostgreSQL in K3D
        axi db down  # Stop local cluster
    """

    EMBEDDING_DIM = 1536  # OpenAI text-embedding-3-small dimension

    def __init__(self, connection_url: str | None = None):
        self.connection_url = connection_url or os.environ.get("AXIOM_DB_URL") or DEFAULT_LOCAL_URL
        self._conn = None
        self._connected = False

    def connect(self) -> None:
        """Establish database connection."""
        if self._connected:
            return

        try:
            import psycopg2

            self._conn = psycopg2.connect(self.connection_url)
            self._connected = True
            self._initialize_schema()
        except ImportError:
            raise RuntimeError(
                "psycopg2 not installed. Run: pip install psycopg2-binary\n"
                "Or for local dev: axi db up"
            )
        except Exception as e:
            raise RuntimeError(
                f"Could not connect to PostgreSQL: {e}\n"
                f"URL: {self._mask_url(self.connection_url)}\n\n"
                "For local development, start the K3D cluster:\n"
                "  axi db up"
            )

    def _mask_url(self, url: str) -> str:
        """Mask password in connection URL for logging."""
        import re

        return re.sub(r":[^:@]+@", ":****@", url)

    def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
            self._connected = False

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        """Get a database cursor with auto-commit."""
        if not self._connected:
            self.connect()
        assert self._conn is not None
        cursor = self._conn.cursor()
        try:
            yield cursor
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cursor.close()

    def _initialize_schema(self) -> None:
        """Create tables and indexes if needed."""
        with self._cursor() as cur:
            # Enable pgvector extension
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")

            # Signals table with vector column
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    embedding vector({self.EMBEDDING_DIM}),
                    signal_type TEXT,
                    initiative TEXT,
                    source TEXT,
                    timestamp TIMESTAMPTZ,
                    metadata JSONB DEFAULT '{{}}',
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    owner_id TEXT,
                    version INTEGER DEFAULT 1
                )
            """)

            # Media table with vector column
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS media (
                    id TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    media_type TEXT,
                    title TEXT,
                    transcript TEXT,
                    transcript_preview TEXT,
                    embedding vector({self.EMBEDDING_DIM}),
                    duration_sec REAL,
                    recorded_at TIMESTAMPTZ,
                    owner_id TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    version INTEGER DEFAULT 1
                )
            """)

            # Participants table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS participants (
                    id TEXT PRIMARY KEY,
                    media_id TEXT NOT NULL REFERENCES media(id) ON DELETE CASCADE,
                    person_id TEXT NOT NULL,
                    name TEXT,
                    role TEXT,
                    access_level TEXT DEFAULT 'participant',
                    mention_count INTEGER DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            # People table (person registry)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS people (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    aliases TEXT[],
                    email TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            # HNSW indexes for fast vector search (cosine similarity)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS signals_embedding_idx
                ON signals USING hnsw (embedding vector_cosine_ops)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS media_embedding_idx
                ON media USING hnsw (embedding vector_cosine_ops)
            """)

            # B-tree indexes for filtering
            cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_type ON signals(signal_type)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_initiative ON signals(initiative)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_media_owner ON media(owner_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_media_recorded ON media(recorded_at)")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_participants_media ON participants(media_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_participants_person ON participants(person_id)"
            )

    # =========================================================================
    # CRUD Operations
    # =========================================================================

    def upsert_signal(self, record: SignalRecord) -> None:
        """Insert or update a signal record."""
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO signals (id, text, embedding, signal_type, initiative, source,
                    timestamp, metadata, owner_id, version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    text = EXCLUDED.text,
                    embedding = EXCLUDED.embedding,
                    signal_type = EXCLUDED.signal_type,
                    initiative = EXCLUDED.initiative,
                    source = EXCLUDED.source,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW(),
                    version = signals.version + 1
            """,
                (
                    record.id,
                    record.text,
                    record.embedding,
                    record.signal_type,
                    record.initiative,
                    record.source,
                    record.timestamp or None,
                    json.dumps(record.metadata),
                    record.owner_id,
                    record.version,
                ),
            )

    def upsert_media(self, record: MediaRecord) -> None:
        """Insert or update a media record."""
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO media (id, path, media_type, title, transcript, transcript_preview,
                    embedding, duration_sec, recorded_at, owner_id, version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    transcript = EXCLUDED.transcript,
                    transcript_preview = EXCLUDED.transcript_preview,
                    embedding = EXCLUDED.embedding,
                    duration_sec = EXCLUDED.duration_sec,
                    updated_at = NOW(),
                    version = media.version + 1
            """,
                (
                    record.id,
                    record.path,
                    record.media_type,
                    record.title,
                    record.transcript,
                    record.transcript_preview,
                    record.embedding,
                    record.duration_sec,
                    record.recorded_at or None,
                    record.owner_id,
                    record.version,
                ),
            )

    def upsert_participant(self, record: ParticipantRecord) -> None:
        """Insert or update a participant record."""
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO participants (id, media_id, person_id, name, role, access_level,
                    mention_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    role = EXCLUDED.role,
                    access_level = EXCLUDED.access_level,
                    mention_count = EXCLUDED.mention_count
            """,
                (
                    record.id,
                    record.media_id,
                    record.person_id,
                    record.name,
                    record.role,
                    record.access_level,
                    record.mention_count,
                ),
            )

    def get_signal(self, signal_id: str) -> SignalRecord | None:
        """Get a signal by ID."""
        with self._cursor() as cur:
            cur.execute("SELECT * FROM signals WHERE id = %s", (signal_id,))
            row = cur.fetchone()
            if not row:
                return None
            return SignalRecord(
                id=row[0],
                text=row[1],
                embedding=list(row[2]) if row[2] else [],
                signal_type=row[3] or "",
                initiative=row[4] or "",
                source=row[5] or "",
                timestamp=str(row[6]) if row[6] else "",
                metadata=row[7] or {},
                created_at=str(row[8]) if row[8] else "",
                updated_at=str(row[9]) if row[9] else "",
                owner_id=row[10] or "",
                version=row[11] or 1,
            )

    def get_media(self, media_id: str) -> MediaRecord | None:
        """Get a media item by ID."""
        with self._cursor() as cur:
            cur.execute("SELECT * FROM media WHERE id = %s", (media_id,))
            row = cur.fetchone()
            if not row:
                return None
            return MediaRecord(
                id=row[0],
                path=row[1],
                media_type=row[2] or "audio",
                title=row[3] or "",
                transcript=row[4] or "",
                transcript_preview=row[5] or "",
                embedding=list(row[6]) if row[6] else [],
                duration_sec=row[7] or 0.0,
                recorded_at=str(row[8]) if row[8] else "",
                owner_id=row[9] or "",
                created_at=str(row[10]) if row[10] else "",
                updated_at=str(row[11]) if row[11] else "",
                version=row[12] or 1,
            )

    # =========================================================================
    # Vector Search
    # =========================================================================

    def search_signals(
        self,
        embedding: list[float],
        top_k: int = 10,
        min_score: float = 0.0,
        filters: dict | None = None,
    ) -> list[SearchResult]:
        """Vector similarity search over signals.

        Uses pgvector HNSW index for fast approximate nearest neighbor search.
        Cosine distance: 0 = identical, 2 = opposite.
        Score = 1 - distance (so higher is better).
        """
        # cosine distance = 1 - cosine similarity
        # We want min_score as similarity, so max_distance = 1 - min_score
        max_distance = 1.0 - min_score

        query = """
            SELECT *, 1 - (embedding <=> %s::vector) as score
            FROM signals
            WHERE embedding IS NOT NULL
        """
        params: list[Any] = [embedding]

        if filters:
            if filters.get("signal_type"):
                query += " AND signal_type = %s"
                params.append(filters["signal_type"])
            if filters.get("initiative"):
                query += " AND initiative = %s"
                params.append(filters["initiative"])
            if filters.get("since"):
                query += " AND timestamp >= %s"
                params.append(filters["since"])
            if filters.get("owner_id"):
                query += " AND owner_id = %s"
                params.append(filters["owner_id"])

        query += " AND (embedding <=> %s::vector) <= %s"
        params.extend([embedding, max_distance])

        query += " ORDER BY embedding <=> %s::vector LIMIT %s"
        params.extend([embedding, top_k])

        with self._cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

        results = []
        for row in rows:
            record = SignalRecord(
                id=row[0],
                text=row[1],
                embedding=list(row[2]) if row[2] else [],
                signal_type=row[3] or "",
                initiative=row[4] or "",
                source=row[5] or "",
                timestamp=str(row[6]) if row[6] else "",
                metadata=row[7] or {},
                created_at=str(row[8]) if row[8] else "",
                updated_at=str(row[9]) if row[9] else "",
                owner_id=row[10] or "",
                version=row[11] or 1,
            )
            score = row[-1]  # Last column is computed score
            results.append(SearchResult(record=record, score=score))

        return results

    def search_media(
        self,
        embedding: list[float],
        top_k: int = 10,
        min_score: float = 0.0,
        accessible_to: str | None = None,
    ) -> list[SearchResult]:
        """Vector similarity search over media with access control."""
        max_distance = 1.0 - min_score

        if accessible_to:
            # Filter to recordings user owns or participates in
            query = """
                SELECT DISTINCT m.*, 1 - (m.embedding <=> %s::vector) as score
                FROM media m
                LEFT JOIN participants p ON m.id = p.media_id
                WHERE m.embedding IS NOT NULL
                AND (m.owner_id = %s OR p.person_id = %s)
                AND (m.embedding <=> %s::vector) <= %s
                ORDER BY m.embedding <=> %s::vector
                LIMIT %s
            """
            params = [
                embedding,
                accessible_to,
                accessible_to,
                embedding,
                max_distance,
                embedding,
                top_k,
            ]
        else:
            query = """
                SELECT *, 1 - (embedding <=> %s::vector) as score
                FROM media
                WHERE embedding IS NOT NULL
                AND (embedding <=> %s::vector) <= %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """
            params = [embedding, embedding, max_distance, embedding, top_k]

        with self._cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

        results = []
        for row in rows:
            record = MediaRecord(
                id=row[0],
                path=row[1],
                media_type=row[2] or "audio",
                title=row[3] or "",
                transcript=row[4] or "",
                transcript_preview=row[5] or "",
                embedding=list(row[6]) if row[6] else [],
                duration_sec=row[7] or 0.0,
                recorded_at=str(row[8]) if row[8] else "",
                owner_id=row[9] or "",
            )
            score = row[-1]
            results.append(SearchResult(record=record, score=score))

        return results

    # =========================================================================
    # Statistics & Health
    # =========================================================================

    def stats(self) -> dict:
        """Get database statistics."""
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM signals")
            signal_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM media")
            media_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM participants")
            participant_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(DISTINCT person_id) FROM participants")
            unique_people = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM signals WHERE embedding IS NOT NULL")
            signals_indexed = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM media WHERE embedding IS NOT NULL")
            media_indexed = cur.fetchone()[0]

        return {
            "backend": "pgvector",
            "signals": signal_count,
            "media": media_count,
            "participants": participant_count,
            "unique_people": unique_people,
            "signals_indexed": signals_indexed,
            "media_indexed": media_indexed,
            "connection": self._mask_url(self.connection_url),
        }

    def health_check(self) -> dict:
        """Check database health and connectivity."""
        try:
            with self._cursor() as cur:
                cur.execute("SELECT version()")
                pg_version = cur.fetchone()[0]

                cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                row = cur.fetchone()
                pgvector_version = row[0] if row else "not installed"

            return {
                "status": "healthy",
                "postgresql": pg_version,
                "pgvector": pgvector_version,
                "connected": True,
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "error": str(e),
                "connected": False,
            }


# ============================================================================
# Compatibility Layer for SignalRAG
# ============================================================================


class PgVectorStore:
    """Drop-in replacement for VectorStore using pgvector backend.

    Maintains API compatibility with the existing VectorStore class
    while using PostgreSQL + pgvector under the hood.
    """

    def __init__(self, db: VectorDB | None = None):
        self._db = db or VectorDB()
        self._db.connect()

        # Cache for chunks (for compatibility)
        self._chunks_cache: dict[str, SignalRecord] = {}

    @property
    def chunks(self) -> list:
        """Return cached chunks for compatibility."""
        from .signal_rag import SignalChunk

        return [
            SignalChunk(
                signal_id=r.id,
                text=r.text,
                timestamp=r.timestamp,
                signal_type=r.signal_type,
                initiative=r.initiative,
                source=r.source,
                metadata=r.metadata,
                embedding=r.embedding,
            )
            for r in self._chunks_cache.values()
        ]

    @property
    def embeddings(self) -> list[list[float]]:
        """Return embeddings for compatibility."""
        return [r.embedding for r in self._chunks_cache.values()]

    def add(self, chunk, embedding: list[float]) -> None:
        """Add a chunk with embedding."""
        record = SignalRecord(
            id=chunk.signal_id,
            text=chunk.text,
            embedding=embedding,
            signal_type=chunk.signal_type,
            initiative=chunk.initiative,
            source=chunk.source,
            timestamp=chunk.timestamp,
            metadata=chunk.metadata,
        )
        self._db.upsert_signal(record)
        self._chunks_cache[record.id] = record

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        min_score: float = 0.0,
    ) -> list[tuple]:
        """Search for similar chunks."""
        results = self._db.search_signals(query_embedding, top_k, min_score)

        from .signal_rag import SignalChunk

        output = []
        for r in results:
            rec = r.record
            if isinstance(rec, SignalRecord):
                output.append(
                    (
                        SignalChunk(
                            signal_id=rec.id,
                            text=rec.text,
                            timestamp=rec.timestamp,
                            signal_type=rec.signal_type,
                            initiative=rec.initiative,
                            source=rec.source,
                            metadata=rec.metadata,
                            embedding=rec.embedding,
                        ),
                        r.score,
                    )
                )
        return output

    def save(self, index_path: Path, embeddings_path: Path) -> None:
        """No-op - data is already persisted in PostgreSQL."""
        pass

    def load(self, index_path: Path, embeddings_path: Path) -> bool:
        """No-op - data is already in PostgreSQL."""
        return True


# ============================================================================
# K3D Cluster Management
# ============================================================================


def _cluster_name() -> str:
    from axiom.infra.branding import get_branding

    return get_branding().cluster_name


K3D_CLUSTER_NAME = _cluster_name()


def get_k3d_config() -> str:
    """Generate K3D cluster configuration."""
    return f"""
apiVersion: k3d.io/v1alpha5
kind: Simple
metadata:
  name: {K3D_CLUSTER_NAME}
servers: 1
agents: 0
ports:
  - port: 5432:5432
    nodeFilters:
      - loadbalancer
  - port: 8080:8080
    nodeFilters:
      - loadbalancer
options:
  k3s:
    extraArgs:
      - arg: --disable=traefik
        nodeFilters:
          - server:*
"""


def get_postgres_manifest() -> str:
    """Generate Kubernetes manifest for PostgreSQL + pgvector."""
    return """
apiVersion: v1
kind: Namespace
metadata:
  name: axiom
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: postgres-config
  namespace: axiom
data:
  POSTGRES_DB: axiom_db
  POSTGRES_USER: axiom
  POSTGRES_PASSWORD: axiom
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: postgres-pvc
  namespace: axiom
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: postgres
  namespace: axiom
spec:
  replicas: 1
  selector:
    matchLabels:
      app: postgres
  template:
    metadata:
      labels:
        app: postgres
    spec:
      containers:
        - name: postgres
          image: pgvector/pgvector:pg16
          ports:
            - containerPort: 5432
          envFrom:
            - configMapRef:
                name: postgres-config
          volumeMounts:
            - name: postgres-storage
              mountPath: /var/lib/postgresql/data
          resources:
            requests:
              memory: "256Mi"
              cpu: "250m"
            limits:
              memory: "1Gi"
              cpu: "1000m"
      volumes:
        - name: postgres-storage
          persistentVolumeClaim:
            claimName: postgres-pvc
---
apiVersion: v1
kind: Service
metadata:
  name: postgres
  namespace: axiom
spec:
  type: LoadBalancer
  ports:
    - port: 5432
      targetPort: 5432
  selector:
    app: postgres
"""


def k3d_up() -> bool:
    """Start local K3D cluster with PostgreSQL + pgvector.

    Returns True if cluster started successfully.
    """
    import subprocess
    import tempfile
    import time

    # Check if k3d is installed
    try:
        subprocess.run(["k3d", "version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: k3d not installed.")
        print("Install with: brew install k3d")
        print("Or: curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash")
        return False

    # Check if cluster already exists
    result = subprocess.run(
        ["k3d", "cluster", "list", "-o", "json"], capture_output=True, text=True
    )
    if K3D_CLUSTER_NAME in result.stdout:
        print(f"Cluster '{K3D_CLUSTER_NAME}' already exists.")
        # Check if running
        if '"running":true' not in result.stdout.lower():
            print("Starting cluster...")
            subprocess.run(["k3d", "cluster", "start", K3D_CLUSTER_NAME], check=True)
    else:
        # Create cluster
        print(f"Creating K3D cluster '{K3D_CLUSTER_NAME}'...")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(get_k3d_config())
            config_path = f.name

        subprocess.run(["k3d", "cluster", "create", "--config", config_path], check=True)

    # Wait for cluster to be ready
    print("Waiting for cluster to be ready...")
    time.sleep(3)

    # Deploy PostgreSQL + pgvector
    print("Deploying PostgreSQL + pgvector...")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(get_postgres_manifest())
        manifest_path = f.name

    subprocess.run(["kubectl", "apply", "-f", manifest_path], check=True)

    # Wait for PostgreSQL to be ready
    print("Waiting for PostgreSQL to be ready...")
    for _ in range(30):
        result = subprocess.run(
            [
                "kubectl",
                "get",
                "pod",
                "-n",
                "axiom",
                "-l",
                "app=postgres",
                "-o",
                "jsonpath={.items[0].status.phase}",
            ],
            capture_output=True,
            text=True,
        )
        if result.stdout.strip() == "Running":
            break
        time.sleep(2)
    else:
        print("Warning: PostgreSQL pod not ready after 60s")

    # Additional wait for PostgreSQL to accept connections
    time.sleep(5)

    print()
    print("✓ Local PostgreSQL + pgvector is ready!")
    print()
    print("Connection URL (already set as default):")
    print(f"  {DEFAULT_LOCAL_URL}")
    print()
    print("To use:")
    print("  from axiom.extensions.builtins.signals.pgvector_store import VectorDB")
    print("  db = VectorDB()")
    print()

    return True


def k3d_down() -> bool:
    """Stop local K3D cluster.

    Returns True if cluster stopped successfully.
    """
    import subprocess

    print(f"Stopping K3D cluster '{K3D_CLUSTER_NAME}'...")

    try:
        subprocess.run(["k3d", "cluster", "stop", K3D_CLUSTER_NAME], check=True)
        print("✓ Cluster stopped")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error stopping cluster: {e}")
        return False


def k3d_delete() -> bool:
    """Delete local K3D cluster and all data.

    Returns True if cluster deleted successfully.
    """
    import subprocess

    print(f"Deleting K3D cluster '{K3D_CLUSTER_NAME}'...")
    print("Warning: This will delete all local data!")

    try:
        subprocess.run(["k3d", "cluster", "delete", K3D_CLUSTER_NAME], check=True)
        print("✓ Cluster deleted")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error deleting cluster: {e}")
        return False


def k3d_status() -> dict:
    """Get K3D cluster status."""
    import subprocess

    try:
        result = subprocess.run(
            ["k3d", "cluster", "list", "-o", "json"], capture_output=True, text=True
        )

        import json as json_mod

        clusters = json_mod.loads(result.stdout) if result.stdout else []

        for cluster in clusters:
            if cluster.get("name") == K3D_CLUSTER_NAME:
                return {
                    "exists": True,
                    "running": cluster.get("serversRunning", 0) > 0,
                    "servers": cluster.get("serversCount", 0),
                    "agents": cluster.get("agentsCount", 0),
                }

        return {"exists": False, "running": False}

    except (subprocess.CalledProcessError, FileNotFoundError):
        return {"exists": False, "running": False, "k3d_installed": False}
    except json.JSONDecodeError:
        return {"exists": False, "running": False}
