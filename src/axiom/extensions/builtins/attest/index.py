# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The log's search index and corpus, derived from gold and searched pre-filtered (unit LD-2).

ADR-251 D1, D3, D4 and D5. One table, ``gold.log_record_index``, keyed by record id and the
embedder's ``(model, dim)``: one row (one chunk) per signed record, read from the gold view
``gold.log_records`` and nothing else. Each chunk carries the record's labels (site, log,
classification, export-control flag, reader roles), its ``kind`` (an ``import`` is labelled so),
the record it acts on (a supplement, acknowledgement, co-signature or seal points at its target),
a text-search column built from the chunk's words with any credential redacted, and the vector the
embedder made of the same words. It holds no copy of the signed bytes: no text is stored, only
lexemes and a vector, so dropping it loses nothing and :func:`rebuild` makes the same rows again.

**One search** (:func:`search`), called by the Timeline, the Log page's search and chat:

1. **The one check first** (:func:`axiom.governance.doors.check`), per log and site the caller
   could mean. A refused log never reaches the database question below. An unnamed caller or a
   policy outage refuses the whole search (:class:`SearchRefused`).
2. **The pre-filter.** The database is asked only about the admitted logs, and only about chunks
   whose labels are the labels the check admitted, together with the structured filters (time
   range, log, entry type, signer, kind, interval). So no hidden row is scored, ranked, counted
   or hinted at: it is never a candidate.
3. **Ranking.** Full-text candidates (``ts_rank``) and semantic candidates (cosine over the
   newest admitted rows in the window) are fused by reciprocal rank fusion (``axiom.rag.rrf``).
4. **Every answer is as of** the gold watermark of its log, and every result links to the record
   through the attest door. Each log that answered writes one read record (X2).

The index is refreshed from gold before each search for the logs being searched (incremental by
sequence) and can be dropped and rebuilt at any time.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    Text,
    cast,
    column,
    func,
    literal,
    or_,
    select,
    table,
    text,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.dialects.postgresql import insert as pg_insert

from axiom.governance import doors as g
from axiom.governance.doors import Caller, Decision, Reason
from axiom.memory.embedding_registry import EmbeddingSpace, EmbeddingSpaceRegistry
from axiom.rag.rrf import reciprocal_rank_fusion

from . import gold, store

log = logging.getLogger("axiom.attest.index")

#: What this indexer reads: gold objects only (LD-2-A1).
INDEX_SOURCES: tuple[str, ...] = (f"gold.{gold.VIEW}",)

#: The index's own table and its per-log progress, in the gold schema.
INDEX_TABLE = "log_record_index"
STATE_TABLE = "log_record_index_state"

#: The labels every chunk carries (ADR-251 D1; ADR-088's chunk contract).
CHUNK_LABELS = ("site_id", "logbook", "classification", "export_controlled", "roles", "kind")

#: Semantic candidates are the newest admitted rows in the window, at most this many.
VECTOR_WINDOW = 2000
#: Text candidates, at most this many, best ``ts_rank`` first.
TEXT_CANDIDATES = 200
#: A semantic candidate below this cosine similarity is not a match.
MIN_SIMILARITY = 0.2
#: Rows read from gold per batch while indexing.
BATCH = 200


class IndexRefused(ValueError):
    """A chunk that may not exist: one without labels or without its embedder."""


class FilterRefused(ValueError):
    """A field filter the search cannot take (LF-A27): refused with the records door's words and
    its wire code, 400 ``filter``."""

    code = "filter"


class SearchRefused(Exception):
    """The whole search is refused: nobody can be named, or the policy cannot decide."""

    def __init__(self, decision: Decision) -> None:
        super().__init__(decision.reason.value if decision.reason else "refused")
        self.decision = decision


# -- the embedder ---------------------------------------------------------------------------

#: The built-in embedder: deterministic feature hashing of the chunk's words, on this machine,
#: so every node has one and nothing leaves it. It is the fallback when the node has no local
#: embedding model; a node may name a model embedder instead.
LEXHASH = EmbeddingSpace(model="axiom-lexhash-v1", dim=256)
MODEL_ENV = "AXIOM_LOG_INDEX_EMBED_MODEL"
DIM_ENV = "AXIOM_LOG_INDEX_EMBED_DIM"
#: ``hashing`` pins the built-in embedder (tests and CI, so results match on every machine).
PIN_ENV = "AXIOM_LOG_INDEX_EMBEDDER"
#: A site's opt-in to a remote embedding provider for log records. Off unless set to a true value.
ALLOW_REMOTE_ENV = "AXIOM_LOG_INDEX_ALLOW_REMOTE_EMBED"
_TRUE = ("1", "true", "yes", "on")

_WORD = re.compile(r"\w+", re.UNICODE)


def lexhash(texts: list[str]) -> list[list[float]]:
    out = []
    for t in texts:
        vec = [0.0] * LEXHASH.dim
        for word in _WORD.findall((t or "").lower()):
            h = hashlib.sha256(word.encode("utf-8")).digest()
            slot = int.from_bytes(h[:4], "big") % LEXHASH.dim
            vec[slot] += 1.0 if h[4] % 2 == 0 else -1.0
        norm = math.sqrt(sum(x * x for x in vec))
        out.append([x / norm for x in vec] if norm else vec)
    return out


def _model_registry(model: str, dim: int) -> EmbeddingSpaceRegistry:
    """A model embedder through the platform's guarded chain, held to this machine or an embedder
    cleared for controlled text; vectors from any other model are refused rather than mixed."""
    from axiom.llm.tier_policy import CONTROLLED
    from axiom.rag.embeddings import embed_texts_with_model

    def embed(texts: list[str]) -> list[list[float]] | None:
        vectors, made_by = embed_texts_with_model(texts, routing_tier=CONTROLLED)
        if not vectors or (made_by or "").split(":")[0] != model.split(":")[0]:
            return None
        return vectors

    return EmbeddingSpaceRegistry(canonical=EmbeddingSpace(model=model, dim=dim), embedder=embed)


_registry: EmbeddingSpaceRegistry | None = None
_resolved: tuple[float, EmbeddingSpaceRegistry] | None = None

#: How long a resolution (a probe of this machine's embedder) is kept before it is made again.
RESOLVE_TTL_SECONDS = 60.0


def set_registry(reg: EmbeddingSpaceRegistry | None) -> None:
    """The embedding space registry the index uses; None returns to the node's default."""
    global _registry, _resolved
    _registry = reg
    _resolved = None


def _local_registry(model: str, dim: int) -> EmbeddingSpaceRegistry:
    """The embedder on this machine alone: no remote provider is ever tried."""
    from axiom.rag.embeddings import embed_texts_local

    return EmbeddingSpaceRegistry(
        canonical=EmbeddingSpace(model=model, dim=dim), embedder=embed_texts_local
    )


def _resolve_default() -> EmbeddingSpaceRegistry:
    """The node's default: its local embedding model when it has one, else the hashing embedder.

    The model is the one the RAG corpus resolves (``axiom.rag.embeddings``'s local step), held to
    this machine. A remote embedder is used only when the site sets ``ALLOW_REMOTE_ENV``, and then
    through the same guarded chain at the controlled tier.
    """
    from axiom.rag import embeddings

    local = embeddings.local_embedding_model()
    if os.environ.get(ALLOW_REMOTE_ENV, "").strip().lower() in _TRUE:
        if local or embeddings.provider_configured(_controlled()):
            return _model_registry(local or _remote_model(), embeddings.local_embedding_dim())
    elif local:
        return _local_registry(local, embeddings.local_embedding_dim())
    return EmbeddingSpaceRegistry(canonical=LEXHASH, embedder=lexhash)


def _controlled() -> str:
    from axiom.llm.tier_policy import CONTROLLED

    return CONTROLLED


def _remote_model() -> str:
    from axiom.llm.local_models import EMBED

    return os.environ.get("AXIOM_RAG_EMBED_MODEL", "").strip() or EMBED


def registry() -> EmbeddingSpaceRegistry:
    """Set explicitly; else the named model (``MODEL_ENV``); else pinned hashing (``PIN_ENV``);
    else the node's local model when it has one; else hashing."""
    global _resolved
    if _registry is not None:
        return _registry
    model = os.environ.get(MODEL_ENV, "").strip()
    if model:
        return _model_registry(model, int(os.environ.get(DIM_ENV, "768") or 768))
    if os.environ.get(PIN_ENV, "").strip().lower() in ("hashing", "lexhash"):
        return EmbeddingSpaceRegistry(canonical=LEXHASH, embedder=lexhash)
    now = time.monotonic()
    if _resolved is None or now - _resolved[0] > RESOLVE_TTL_SECONDS:
        _resolved = (now, _resolve_default())
    return _resolved[1]


# -- the tables -----------------------------------------------------------------------------

_ensured: set[str] = set()
_lock = threading.Lock()


def _t(name: str) -> str:
    """``"gold"."<name>"``: the schema and the table each validated and quoted by
    :func:`gold.ident`, which refuses anything but a plain identifier."""
    return f"{gold.ident(gold.schema())}.{gold.ident(name)}"


_TYPES: dict[str, Any] = {
    "dim": Integer, "seq": BigInteger, "interval_number": BigInteger,
    "occurred_at": DateTime(timezone=True), "recorded_at": DateTime(timezone=True),
    "as_of": DateTime(timezone=True), "export_controlled": Boolean, "redacted": Boolean,
    "roles": ARRAY(Text), "secret_patterns": ARRAY(Text), "search_text": TSVECTOR,
    "embedding": ARRAY(Float), "warm_until": DateTime(timezone=True),
}  # fmt: skip


def _table(name: str, columns: Sequence[str]) -> Any:
    """A Core table clause in the gold schema, so reads and writes build no SQL text."""
    gold.ident(name)
    return table(name, *(column(c, _TYPES.get(c, Text)) for c in columns), schema=gold.schema())


def _index() -> Any:
    return _table(INDEX_TABLE, _COLUMNS)


def _state() -> Any:
    return _table(STATE_TABLE, ("site_id", "logbook", "model", "dim", "seq", "as_of"))


def _ddl() -> list[str]:
    idx, state = _t(INDEX_TABLE), _t(STATE_TABLE)
    return [
        f"""CREATE TABLE IF NOT EXISTS {idx} (
            attestation_id text NOT NULL,
            model text NOT NULL CHECK (model <> ''),
            dim integer NOT NULL CHECK (dim > 0),
            site_id text NOT NULL,
            logbook text NOT NULL,
            seq bigint NOT NULL,
            kind text NOT NULL,
            meaning text,
            entry_type text,
            signer text,
            occurred_at timestamptz NOT NULL,
            recorded_at timestamptz,
            target text,
            interval_kind text,
            interval_number bigint,
            classification text NOT NULL CHECK (classification <> ''),
            export_controlled boolean NOT NULL,
            roles text[] NOT NULL,
            redacted boolean NOT NULL,
            secret_patterns text[] NOT NULL,
            chunk_sha256 text NOT NULL,
            search_text tsvector NOT NULL,
            embedding double precision[],
            PRIMARY KEY (attestation_id, model, dim))""",
        f"CREATE INDEX IF NOT EXISTS {gold.ident(INDEX_TABLE + '_text')} ON {idx} USING gin (search_text)",
        f"CREATE INDEX IF NOT EXISTS {gold.ident(INDEX_TABLE + '_time')} ON {idx} "
        "(site_id, logbook, model, dim, occurred_at DESC)",
        # Unit LD-8: a row rehydrated from an archived period is warm until ``warm_until``; a
        # retired period's summary records what its lexemes were ``folded`` from.
        f"ALTER TABLE {idx} ADD COLUMN IF NOT EXISTS warm_until timestamptz",
        f"ALTER TABLE {idx} ADD COLUMN IF NOT EXISTS folded text",
        f"""CREATE TABLE IF NOT EXISTS {state} (
            site_id text NOT NULL,
            logbook text NOT NULL,
            model text NOT NULL,
            dim integer NOT NULL,
            seq bigint NOT NULL,
            as_of timestamptz,
            PRIMARY KEY (site_id, logbook, model, dim))""",
    ]


def ensure(session: Any) -> None:
    gold.ensure(session)
    key = gold.schema()
    if key in _ensured:
        return
    with _lock:
        if key in _ensured:
            return
        for stmt in _ddl():
            # nosemgrep: avoid-sqlalchemy-text -- DDL: schema, table and index names passed gold.ident(); no value
            session.execute(text(stmt))
        for name in (INDEX_TABLE, STATE_TABLE):
            session.connection().exec_driver_sql(
                f"COMMENT ON TABLE {_t(name)} IS {gold.comment_literal()}")
        session.commit()
        _ensured.add(key)


def forget() -> None:
    _ensured.clear()


# -- one record, one chunk ------------------------------------------------------------------


def reader_roles(book: Any) -> list[str]:
    """The roles that may read a log's records (its ``readers`` and its signers); ``*`` when
    the log names no readers, which any role at the site may read."""
    if not getattr(book, "readers", ()):
        return ["*"]
    return sorted(set(book.readers) | {r for t in book.types for r in t.roles})


def chunk_text(content: Any) -> str:
    """The searchable words of one record: its title, a seal's summary, its body and field
    values, in that order."""
    content = content if isinstance(content, dict) else {}
    summary = content.get("summary")
    parts = [str(content.get("title") or ""), summary if isinstance(summary, str) else "",
             str(content.get("body") or "")]
    fields = content.get("fields") or {}
    if isinstance(fields, dict):
        for key in sorted(fields):
            value = fields[key]
            shown = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
            parts.append(f"{key}: {shown}")
    return "\n".join(p for p in parts if p)


@dataclass(frozen=True)
class Chunk:
    row: dict[str, Any]
    text: str
    patterns: tuple[str, ...]
    labels: dict[str, Any]
    roles: tuple[str, ...]
    space: EmbeddingSpace


_CANONICAL: Any = object()


def chunk_for(row: dict[str, Any], *, labels: dict[str, Any] | None, roles: Iterable[str],
              space: Any = _CANONICAL) -> Chunk:  # fmt: skip
    """One record's chunk. Refused without valid labels or without an embedder (ADR-251 D3)."""
    if not g.labels_valid(labels):
        raise IndexRefused(f"record {row.get('attestation_id')}: no labels, so no chunk")
    if space is _CANONICAL:
        space = registry().canonical
    if not isinstance(space, EmbeddingSpace):
        raise IndexRefused(f"record {row.get('attestation_id')}: no embedder, so no chunk")
    from axiom.infra.secret_scan import redact

    words, patterns = redact(chunk_text(row.get("content")))
    return Chunk(row=row, text=words, patterns=tuple(patterns), labels=dict(labels or {}),
                 roles=tuple(roles), space=space)  # fmt: skip


def _record_exposure(chunk: Chunk, state_dir: Path | None) -> None:
    """A credential in a signed record is an exposure, recorded on the vault's exposure path
    (``axi vault exposed``). Names the record and the pattern, never the value. No reference is
    known to rotate, so a person rotates whatever it was."""
    from axiom.infra.state import locked_append_jsonl

    try:
        if state_dir is None:
            from axiom.infra.paths import get_user_state_dir

            state_dir = get_user_state_dir()
        path = Path(state_dir) / "secrets" / "exposures.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = chunk.row
        for pattern in chunk.patterns:
            locked_append_jsonl(path, {
                "event": "exposure", "ts": time.time(), "ref": None, "pattern": pattern,
                "where": f"log index: {row['site_id']}/{row['logbook']}#{row['attestation_id']}",
                "detail": "a credential-shaped string in a signed record; indexed redacted, the "
                          "record is unchanged; rotate the credential it was",
            })  # fmt: skip
    except Exception as exc:  # noqa: BLE001 - the index row is redacted either way
        log.warning("log index: exposure not recorded: %s", exc)


def _interval(row: dict[str, Any]) -> tuple[str | None, int | None]:
    iv = row.get("interval")
    if not isinstance(iv, dict):
        return None, None
    try:
        number = int(iv["number"]) if iv.get("number") is not None else None
    except (TypeError, ValueError):
        number = None
    return (str(iv["kind"]) if iv.get("kind") else None), number


def _params(chunk: Chunk, vector: list[float] | None) -> dict[str, Any]:
    row = chunk.row
    kind, number = _interval(row)
    return {
        "attestation_id": row["attestation_id"], "model": chunk.space.model, "dim": chunk.space.dim,
        "site_id": row["site_id"], "logbook": row["logbook"], "seq": int(row["seq"]),
        "kind": row.get("kind") or "statement", "meaning": row.get("meaning"),
        "entry_type": row.get("entry_type"), "signer": row.get("signer"),
        "occurred_at": row["occurred_at"], "recorded_at": row.get("recorded_at"),
        "target": row.get("target"), "interval_kind": kind, "interval_number": number,
        "classification": chunk.labels["classification"],
        "export_controlled": bool(chunk.labels["export_controlled"]),
        "roles": list(chunk.roles), "redacted": bool(chunk.patterns),
        "secret_patterns": list(chunk.patterns),
        "chunk_sha256": hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
        "search_text": func.to_tsvector(literal("simple"), chunk.text), "embedding": vector,
    }  # fmt: skip


def _book(logbook: str) -> Any:
    from . import registry as books
    from .logbooks import LogbookError

    try:
        return books.get(logbook)
    except LogbookError:
        return None


def _index_log(s: Any, site: str, logbook: str, state_dir: Path | None) -> None:
    from . import doors as log_doors

    labels = log_doors.labels_for(logbook)
    book = _book(logbook)
    if book is None or not g.labels_valid(labels):
        return  # unlabeled: never indexed, never served
    roles = reader_roles(book)
    reg = registry()
    space = reg.canonical
    idx, st, v = _index(), _state(), gold.view()
    mine = (idx.c.site_id == site, idx.c.logbook == logbook, idx.c.model == space.model,
            idx.c.dim == space.dim)  # fmt: skip
    cls, ec = str(labels["classification"]), bool(labels["export_controlled"])  # type: ignore[index]
    # A log whose declaration changed carries its labels now: the check admits by today's labels.
    s.execute(update(idx).where(*mine, or_(idx.c.classification != cls,
                                           idx.c.export_controlled != ec,
                                           idx.c.roles != cast(roles, ARRAY(Text))))
              .values(classification=cls, export_controlled=ec, roles=roles))  # fmt: skip
    last = s.execute(select(st.c.seq).where(
        st.c.site_id == site, st.c.logbook == logbook, st.c.model == space.model,
        st.c.dim == space.dim)).scalar() or 0  # fmt: skip
    while True:
        rows = [dict(r._mapping) for r in s.execute(
            select(v).where(v.c.site_id == site, v.c.logbook == logbook, v.c.seq > last)
            .order_by(v.c.seq).limit(BATCH))]  # fmt: skip
        if not rows:
            break
        last_seen = int(rows[-1]["seq"])
        # A record whose content was removed under its retention rule (a tombstone, unit LD-6) is
        # never indexed again: its row went with its content, and a rebuild does not bring it back.
        rows = [r for r in rows if r.get("content") is not None]
        if not rows:
            last = last_seen
            continue
        chunks = [chunk_for(r, labels=labels, roles=roles, space=space) for r in rows]
        vectors = reg.embed([c.text for c in chunks]) or [None] * len(chunks)
        for chunk, vector in zip(chunks, vectors, strict=True):
            made = s.execute(
                pg_insert(idx).values(**_params(chunk, vector))
                .on_conflict_do_nothing(index_elements=["attestation_id", "model", "dim"])
                .returning(idx.c.attestation_id)).first()  # fmt: skip
            if made is not None and chunk.patterns:
                _record_exposure(chunk, state_dir)
        last = last_seen
    # A retired period is its seal's summary only; warm rows past their window go (unit LD-8).
    from . import summary_index

    summary_index.compact(s, site, logbook, space=space, labels=labels, roles=roles)  # type: ignore[arg-type]
    as_of = s.execute(select(func.max(v.c.recorded_at))
                      .where(v.c.site_id == site, v.c.logbook == logbook)).scalar()  # fmt: skip
    up = pg_insert(st).values(site_id=site, logbook=logbook, model=space.model, dim=space.dim,
                              seq=last, as_of=as_of)  # fmt: skip
    s.execute(up.on_conflict_do_update(
        index_elements=["site_id", "logbook", "model", "dim"],
        set_={"seq": func.greatest(up.excluded.seq, st.c.seq), "as_of": up.excluded.as_of}))


def remove_records(s: Any, ids: Iterable[str]) -> int:
    """Delete every index row of the records ``ids`` (their content was removed under a tombstone,
    unit LD-6), in the caller's transaction. Returns how many rows went."""
    wanted = list(ids)
    if not wanted or not _exists(s):
        return 0
    from sqlalchemy import delete

    idx = _index()
    return int(s.execute(delete(idx).where(idx.c.attestation_id.in_(wanted))).rowcount or 0)


def count_records(s: Any, ids: Iterable[str]) -> int:
    """How many index rows the records ``ids`` have (for a dry run)."""
    wanted = list(ids)
    if not wanted or not _exists(s):
        return 0
    idx = _index()
    return int(s.execute(select(func.count()).select_from(idx).where(idx.c.attestation_id.in_(wanted))).scalar() or 0)


def refresh(pairs: Iterable[tuple[str, str]] | None = None, *, state_dir: Path | None = None) -> None:
    """Bring the index up to gold for ``(site, log)`` pairs (every pair gold holds by default)."""
    with store.session_scope() as s:
        ensure(s)
        if pairs is None:
            v = gold.view()
            pairs = [tuple(r) for r in s.execute(
                select(v.c.site_id, v.c.logbook).distinct().order_by(v.c.site_id, v.c.logbook))]
        for site, logbook in pairs:
            _index_log(s, site, logbook, state_dir)
        s.commit()


def drop() -> None:
    """Drop the index. Nothing official is lost: every row is derived from gold."""
    with store.session_scope() as s:
        for name in (INDEX_TABLE, STATE_TABLE):
            # nosemgrep: avoid-sqlalchemy-text -- DDL: schema and table names passed gold.ident(); no value
            s.execute(text(f"DROP TABLE IF EXISTS {_t(name)}"))
        s.commit()
    forget()


def rebuild(*, state_dir: Path | None = None) -> str:
    """Drop and rebuild from gold; returns :func:`digest` of the rows made."""
    drop()
    refresh(state_dir=state_dir)
    return digest()


_COLUMNS = ("attestation_id", "model", "dim", "site_id", "logbook", "seq", "kind", "meaning",
            "entry_type", "signer", "occurred_at", "recorded_at", "target", "interval_kind",
            "interval_number", "classification", "export_controlled", "roles", "redacted",
            "secret_patterns", "chunk_sha256", "search_text", "embedding", "warm_until",
            "folded")  # fmt: skip


def _exists(s: Any) -> bool:
    return s.execute(text("SELECT to_regclass(:t)"), {"t": _t(INDEX_TABLE)}).scalar() is not None


def digest() -> str:
    """A digest over every index row, in key order: equal digests, equal indexes."""
    h = hashlib.sha256()
    with store.session_scope() as s:
        if _exists(s):
            idx = _index()
            for row in s.execute(select(*(cast(idx.c[c], Text) for c in _COLUMNS))
                                 .order_by(idx.c.attestation_id, idx.c.model, idx.c.dim)):  # fmt: skip
                h.update(json.dumps(list(row), default=str).encode("utf-8"))
    return h.hexdigest()


# -- the one search -------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    attestation_id: str
    site_id: str
    logbook: str
    occurred_at: Any
    text_rank: float | None = None
    embedding: Sequence[float] | None = None


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _rank(text_hits: list[Candidate], vector_hits: list[Candidate], qvec: Sequence[float] | None,
          limit: int) -> list[tuple[str, float]]:  # fmt: skip
    """Fuse the full-text and semantic candidates by reciprocal rank fusion. Every candidate here
    already passed the one check and the filters: this function is never handed a hidden row."""
    text_order = [c.attestation_id for c in text_hits]
    semantic: list[tuple[float, Candidate]] = []
    if qvec is not None:
        for c in vector_hits:
            sim = _cosine(qvec, c.embedding or ())
            if sim >= MIN_SIMILARITY:
                semantic.append((sim, c))
    semantic.sort(key=lambda p: (-p[0], -_ts(p[1].occurred_at), p[1].attestation_id))
    fused = reciprocal_rank_fusion([text_order, [c.attestation_id for _, c in semantic]], limit=limit)
    return [(str(f.doc_id), float(f.score)) for f in fused]


def _ts(value: Any) -> float:
    return value.timestamp() if isinstance(value, datetime) else 0.0


def _when(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    from datetime import UTC

    got = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return got if got.tzinfo else got.replace(tzinfo=UTC)


def _as_list(value: Any) -> list[str] | None:
    if value in (None, "", [], ()):
        return None
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v) for v in value]


def _sites(caller: Caller, site: str | None, sites: Iterable[str] | None, s: Any) -> list[str]:
    """The sites a search may look at: what was asked, never wider than the caller's scope."""
    if site:
        return [site] if caller.may_see_site(site) else []
    if sites is not None:
        return sorted(x for x in sites if caller.may_see_site(x))
    if caller.sites is not None:
        return sorted(caller.sites)
    from axiom.infra.site_scope import deployment_sites

    served = deployment_sites()
    if served is not None:
        return sorted(served)
    v = gold.view()
    return [r[0] for r in s.execute(select(v.c.site_id).distinct().order_by(v.c.site_id))]


def _filters(idx: Any, f: dict[str, Any]) -> list[Any]:
    """The structured filters as Core clauses on the index (every value bound)."""
    out: list[Any] = []
    if f.get("since") is not None:
        out.append(idx.c.occurred_at >= f["since"])
    if f.get("until") is not None:
        out.append(idx.c.occurred_at <= f["until"])
    if f.get("entry_types"):
        out.append(idx.c.entry_type.in_(f["entry_types"]))
    if f.get("signer"):
        out.append(idx.c.signer == f["signer"])
    if f.get("kinds"):
        out.append(idx.c.kind.in_(f["kinds"]))
    if f.get("exclude_kinds"):
        out.append(idx.c.kind.not_in(f["exclude_kinds"]))
    if f.get("interval"):
        kind, _, number = str(f["interval"]).partition(":")
        out.append(idx.c.interval_kind == kind)
        if number:
            out.append(idx.c.interval_number == int(number))
    return out


def _field_predicates(fields: Mapping[str, Any], logbook: str | None) -> tuple[Any, ...]:
    from .filters import parse_field_filters
    from .logbooks import LogbookError

    try:
        return parse_field_filters(fields, logbook) or ()
    except (ValueError, LogbookError) as exc:
        raise FilterRefused(str(exc)) from None


def _field_where(idx: Any, predicates: Sequence[Any]) -> list[Any]:
    """The declared field filters as conditions of the one pre-filter (LF-A27), read from the
    field index and ANDed with everything :func:`_filters` adds. A summary standing for a retired
    period (unit LD-8) is never a candidate under them: its words are folded from every entry of
    the period, so it cannot say that the entry it would surface is one the filter admits."""
    from . import field_index

    if not predicates:
        return []
    return [idx.c.folded.is_(None), *(field_index.search_clause(idx, p) for p in predicates)]


def _window(text_: str, needle: str, width: int = 160) -> str:
    flat = " ".join(str(text_ or "").split())
    words = _WORD.findall(needle.lower())
    at = min((p for p in (flat.lower().find(w) for w in words) if p >= 0), default=-1)
    start = max(0, at - width // 3) if at >= 0 else 0
    end = min(len(flat), start + width)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


def search(
    caller: Caller,
    query: str,
    *,
    door_id: str = "attest.search",
    transport: str = "agent_tool",
    site: str | None = None,
    sites: Iterable[str] | None = None,
    logbook: str | None = None,
    since: Any = None,
    until: Any = None,
    entry_types: Any = None,
    signer: str | None = None,
    kinds: Any = None,
    exclude_kinds: Any = None,
    interval: str | None = None,
    limit: int = 20,
    state_dir: Path | None = None,
    fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The one search over the log's records: pre-filtered, hybrid, time-first, as of gold.

    ``fields`` are one log's declared field filters (``field.<id>`` keys or ``where``, as the
    records door takes them; ADR-268, LF-A27). They need ``logbook``, are read against its
    declaration after the one check, and join the one pre-filter, so an entry outside them is
    never a candidate. Anything else is :class:`FilterRefused` with the records door's words."""
    from axiom.infra.secret_scan import redact

    from . import archives, summary_index
    from . import doors as log_doors
    from . import registry as books
    from .filters import has_field_filters
    from .service import text_query

    archives.forget_source()  # a new read: no package has been read for this search yet
    limit = max(1, min(int(limit or 20), 50))
    door = g.get(door_id)
    if not caller.resolved:
        raise SearchRefused(Decision(False, Reason.UNRESOLVED_CALLER))
    query = " ".join(str(query or "").split())
    fields = dict(fields or {})
    if fields and not has_field_filters(fields):
        fields = {}
    if fields and not logbook:
        raise FilterRefused("field filters narrow one log's records: give logbook=<id> with them")
    out: dict[str, Any] = {"query": query, "as_of": None, "results": [], "count": 0}
    if not query:
        return out
    state_dir = state_dir or getattr(caller, "state_dir", None)
    filters = {
        "since": _when(since), "until": _when(until), "entry_types": _as_list(entry_types),
        "signer": signer or None, "kinds": _as_list(kinds),
        "exclude_kinds": _as_list(exclude_kinds), "interval": interval or None,
    }  # fmt: skip
    with store.session_scope() as s:
        ensure(s)
        pool = _sites(caller, site, sites, s)
    # 1. The one check, per log and site, before anything is read.
    admitted: list[tuple[str, str, Decision, g.Target]] = []
    for book in books.all_logbooks():
        if logbook and book.id != logbook:
            continue
        for st in pool:
            target = log_doors.target(st, book.id)
            decision = g.check(door, caller, target)
            if decision.reason is Reason.POLICY_UNAVAILABLE:
                raise SearchRefused(decision)
            if decision.allowed:
                admitted.append((st, book.id, decision, target))
    if not admitted:
        return out
    predicates = _field_predicates(fields, logbook) if fields else ()
    if predicates:
        # A person who has just signed finds the entry with the filter they are using (LF-A11).
        from . import field_index

        for st, b, _, _ in admitted:
            field_index.catch_up(st, b)
    refresh([(st, b) for st, b, _, _ in admitted], state_dir=state_dir)
    reg = registry()
    space = reg.canonical
    qvecs = reg.embed_query([query])
    qvec = qvecs[0] if qvecs else None
    pattern = text_query(query)
    idx = _index()
    # 2. The pre-filter: only admitted logs, only chunks with the labels the check admitted, and
    # the structured filters, all in the one WHERE every candidate query shares.
    admitted_keys = [(a[0], a[1], str(a[2].labels["classification"]),  # type: ignore[index]
                      bool(a[2].labels["export_controlled"])) for a in admitted]  # type: ignore[index]
    where = [
        tuple_(idx.c.site_id, idx.c.logbook, idx.c.classification, idx.c.export_controlled)
        .in_(admitted_keys),
        idx.c.model == space.model, idx.c.dim == space.dim, *_filters(idx, filters),
        summary_index.live_clause(idx),  # a warm row past its window is never ranked (unit LD-8)
        *_field_where(idx, predicates),
    ]  # fmt: skip
    head = (idx.c.attestation_id, idx.c.site_id, idx.c.logbook, idx.c.occurred_at)
    with store.session_scope() as s:
        text_hits: list[Candidate] = []
        if pattern:
            q = func.to_tsquery(literal("simple"), pattern)
            rank = func.ts_rank(idx.c.search_text, q).label("tr")
            rows = s.execute(
                select(*head, rank).where(*where, idx.c.search_text.op("@@")(q))
                .order_by(rank.desc(), idx.c.occurred_at.desc(), idx.c.attestation_id)
                .limit(TEXT_CANDIDATES))  # fmt: skip
            text_hits = [Candidate(r[0], r[1], r[2], r[3], text_rank=float(r[4])) for r in rows]
        vector_hits: list[Candidate] = []
        if qvec is not None:
            rows = s.execute(
                select(*head, idx.c.embedding).where(*where, idx.c.embedding.is_not(None))
                .order_by(idx.c.occurred_at.desc(), idx.c.attestation_id).limit(VECTOR_WINDOW))  # fmt: skip
            vector_hits = [Candidate(r[0], r[1], r[2], r[3], embedding=r[4]) for r in rows]
        # 3. Ranking, over admitted candidates only.
        ranked = _rank(text_hits, vector_hits, qvec, limit) if (text_hits or vector_hits) else []
        wanted = [aid for aid, _ in ranked]
        shown = {}
        if wanted:
            v = gold.view()
            for r in s.execute(select(
                    v.c.attestation_id, v.c.site_id, v.c.logbook, v.c.seq, v.c.kind, v.c.meaning,
                    v.c.entry_type, v.c.signer, v.c.occurred_at, v.c.target, v.c.content,
            ).where(v.c.attestation_id.in_(wanted))):  # fmt: skip
                shown[r[0]] = dict(r._mapping)
        marks = {}
        state = _state()
        for st, b, _, _ in admitted:
            marks[(st, b)] = s.execute(select(state.c.as_of).where(
                state.c.site_id == st, state.c.logbook == b, state.c.model == space.model,
                state.c.dim == space.dim)).scalar()  # fmt: skip
    # An admitted result in an archived period shows its content from the package (unit LD-5).
    shown = {k: archives.overlay_gold(v) for k, v in shown.items()}
    by_pair = {(a[0], a[1]): a for a in admitted}
    results = []
    for rank, (aid, score) in enumerate(ranked, start=1):
        row = shown.get(aid)
        if row is None or (row["site_id"], row["logbook"]) not in by_pair:
            continue
        content = row.get("content") or {}
        title, _ = redact(str((content or {}).get("title") or ""))
        words, _ = redact(chunk_text(content))
        st, b = row["site_id"], row["logbook"]
        results.append({
            "attestation_id": aid, "site": st, "logbook": b, "seq": row["seq"],
            "kind": row.get("kind") or "statement", "meaning": row.get("meaning"),
            "entry_type": row.get("entry_type"), "signer": row.get("signer"),
            "occurred_at": gold.iso(row.get("occurred_at")), "target": row.get("target"),
            "title": title, "snippet": _window(words, query),
            "score": score, "rank": rank, "as_of": gold.iso(marks.get((st, b))),
            "link": f"/api/v1/attest/{b}/records/{aid}?site={st}", "url": f"#/log/{b}/{aid}",
            "matched": "name" if query.lower() in title.lower() else "contents",
        })  # fmt: skip
    # Each result says whether it came from an archived period, and whether it is the summary
    # standing for a retired one (unit LD-8).
    summary_index.mark(results)
    # 4. One read record per log that answered, at the door that served it.
    for (st, b), (_, _, decision, target) in ((k, by_pair[k]) for k in by_pair):
        got = [r["attestation_id"] for r in results if (r["site"], r["logbook"]) == (st, b)]
        if got:
            log_doors.record(door.id, caller, target, decision, transport=transport, ids=got,
                             count=len(got), params={"logbook": b, "site": st},
                             state_dir=state_dir)  # fmt: skip
    marks_seen = [m for (k, m) in marks.items() if m is not None]
    out.update(results=results, count=len(results),
               as_of=gold.iso(max(marks_seen)) if marks_seen else None)  # fmt: skip
    return out


# -- for tests ------------------------------------------------------------------------------


def rows_for_tests() -> list[dict[str, Any]]:
    with store.session_scope() as s:
        if not _exists(s):
            return []
        idx = _index()
        return [dict(r._mapping) for r in s.execute(
            select(*(idx.c[c] for c in _COLUMNS if c != "search_text"))
            .order_by(idx.c.site_id, idx.c.logbook, idx.c.seq, idx.c.model, idx.c.dim))]  # fmt: skip


def lexemes_for_tests(attestation_id: str) -> list[str]:
    with store.session_scope() as s:
        idx = _index()
        got = s.execute(select(cast(idx.c.search_text, Text))
                        .where(idx.c.attestation_id == attestation_id)).scalar()  # fmt: skip
    return re.findall(r"'((?:[^']|'')*)'", got or "")


__all__ = [
    "CHUNK_LABELS",
    "INDEX_SOURCES",
    "INDEX_TABLE",
    "LEXHASH",
    "Candidate",
    "Chunk",
    "FilterRefused",
    "IndexRefused",
    "SearchRefused",
    "chunk_for",
    "chunk_text",
    "count_records",
    "digest",
    "drop",
    "ensure",
    "forget",
    "lexhash",
    "rebuild",
    "reader_roles",
    "refresh",
    "registry",
    "remove_records",
    "search",
    "set_registry",
]
