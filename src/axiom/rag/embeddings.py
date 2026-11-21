# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Embedding generation with automatic provider fallback.

Provider chain:
1. Remote embed endpoint (AXIOM_RAG_EMBED_URL env var) — any OpenAI-compatible /v1/embeddings server
   e.g. a nomic-embed-text llama.cpp instance on a remote VPN host
2. OpenAI API (text-embedding-3-small) — if OPENAI_API_KEY set and quota available
3. Ollama local (nomic-embed-text) — free, offline, no API key needed
4. None — caller handles fallback (e.g., keyword search)

Every provider passes the guard first (RS-E2): text goes to another machine
only when the node's agent policy allows model calls here and that machine is
cleared for the text's routing tier, read from the ``routing_tier`` the
gateway's llm-providers.toml declares for its host. This machine's own Ollama
is always admitted. A retrieval query is embedded through :func:`embed_query`
alone, at the session's tier.

Environment variables:
  AXIOM_RAG_EMBED_URL    Base URL of an OpenAI-compatible embedding server
                         e.g. https://<vpn-host>:42000
  AXIOM_RAG_EMBED_MODEL  Model name to request (default: nomic-embed-text)
  AXIOM_RAG_EMBED_KEY    API key for the remote endpoint (optional)
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from axiom.infra.brand_migration import getenv
from axiom.llm.local_models import EMBED as _EMBED_PIN

log = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    """Base: a configured embedding provider was tried and failed.

    Carries the real upstream ``status`` + ``body`` when available so callers
    (and humans) see the actual reason instead of a guessed string.
    """

    def __init__(self, message: str, *, status: int | None = None,
                 body: str | None = None, provider: str | None = None) -> None:
        self.status = status
        self.body = body
        self.provider = provider
        detail = ""
        if provider:
            detail += f" [{provider}]"
        if status is not None:
            detail += f" HTTP {status}"
        if body:
            detail += f": {body[:300]}"
        super().__init__(message + detail)


class TransientEmbeddingError(EmbeddingError):
    """Retryable failure — network drop, timeout, 429, 5xx. Caller may retry
    later (don't persist text-only; the item will succeed on re-run)."""


class PersistentEmbeddingError(EmbeddingError):
    """Non-retryable failure — the provider rejected the INPUT (4xx, e.g.
    400 invalid/too-long). Retrying is futile; the caller must QUARANTINE the
    item with this reason rather than retry it forever."""


_OPENAI_API_URL = "https://api.openai.com/v1/embeddings"
# The RAG store's embedding column holds 768 numbers; OpenAI's text-embedding-3 models return 1,536 unless asked.
_STORE_DIMENSIONS = 768
_OLLAMA_API_URL = "http://localhost:11434/api/embed"
_OLLAMA_EMBED_MODEL = _EMBED_PIN  # axiom.llm.local_models.EMBED, the one place

_BATCH_SIZE = 100
_MAX_RETRIES = 3
_BACKOFF_BASE = 2.0

# Input-bounding (prevent-at-source). Strip control / non-printable bytes that
# OCR'd text carries (the embedder 400s on them), drop invalid UTF-8, and cap
# per-text length well under the model's context window.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_TEXT_CHARS = 8000
# Statuses that mean "the INPUT is bad" → persistent (do not retry).
_PERSISTENT_STATUSES = frozenset({400, 413, 422})


def sanitize_for_embedding(text: str) -> str:
    """Strip control/non-UTF8 bytes and cap length so the embedder never 400s
    on pathological (e.g. OCR-garbage) content. Pure; safe to call anywhere."""
    t = _CONTROL_CHARS.sub("", text or "")
    t = t.encode("utf-8", "ignore").decode("utf-8", "ignore")
    if len(t) > _MAX_TEXT_CHARS:
        t = t[:_MAX_TEXT_CHARS]
    return t

# Track whether OpenAI is quota-blocked this session (don't retry every call)
_openai_quota_blocked = False

#: ``(id of the vectors, model)`` for this context's last embedding call that returned vectors.
_embedded_with: contextvars.ContextVar[tuple[int, str] | None] = contextvars.ContextVar(
    "rag_embedded_with", default=None
)


def embedded_with(vectors: list[list[float]] | None) -> str | None:
    """The model that made ``vectors``, when they are what this context's last :func:`embed_texts` returned.

    For a writer that calls ``embed_texts`` and must declare the model to the
    store (``upsert_chunks(embedding_model=)``). ``None`` for no vectors, and
    for vectors that did not come from the platform chain (a test double): the
    store then accepts the write as it always has.
    """
    hit = _embedded_with.get()
    if not vectors or hit is None or hit[0] != id(vectors):
        return None
    return hit[1]


def embed_texts(
    texts: list[str],
    model: str = "text-embedding-3-small",
    *,
    routing_tier: str = "any",
) -> list[list[float]] | None:
    """Embed texts using the best provider admitted for ``routing_tier``.

    Priority: remote AXIOM_RAG_EMBED_URL → OpenAI → local Ollama → None, each
    only when :func:`embedder_admitted` lets ``routing_tier`` content go there.
    Returns None if no admitted provider is available (caller falls back to
    keyword search).
    """
    return embed_texts_with_model(texts, model, routing_tier=routing_tier)[0]


def embedder_admitted(url: str, routing_tier: str = "any") -> bool:
    """Whether text of ``routing_tier`` may be sent to the embedder at ``url`` (RS-E2).

    The same rule the gateway applies to a completion: this machine's own
    embedder always; another machine only when the node's agent policy allows
    model calls here, and only for content that machine is cleared for, read
    from the ``routing_tier`` the gateway's llm-providers.toml declares for its
    host (``axiom.llm.tier_policy.endpoint_admits``).
    """
    from axiom.llm.tier_policy import endpoint_admits, on_this_machine

    if on_this_machine(url):
        return True
    if not _model_calls_may_leave():
        return False
    return endpoint_admits(url, routing_tier)


def _model_calls_may_leave() -> bool:
    """False when the site's agent policy (ADR-164 node file) forbids model calls here.

    Such a node still embeds on this machine; nothing goes to another one. An
    unreadable node file never locks anyone out, as for completions.
    """
    try:
        from axiom.infra import node_functions as _nf

        return _nf.llm_calls_allowed(_nf.load())
    except Exception:  # noqa: BLE001
        return True


def _remote_url() -> str:
    return getenv("AXIOM_RAG_EMBED_URL", "").rstrip("/")


def refused_embedders(routing_tier: str = "any") -> list[str]:
    """The configured embedders that ``routing_tier`` text may not be sent to, by name."""
    refused = []
    remote = _remote_url()
    if remote and not embedder_admitted(remote, routing_tier):
        refused.append(f"the embedder at {remote}")
    if os.environ.get("OPENAI_API_KEY") and not embedder_admitted(_OPENAI_API_URL, routing_tier):
        refused.append("OpenAI")
    return refused


def embed_texts_with_model(
    texts: list[str],
    model: str = "text-embedding-3-small",
    *,
    routing_tier: str = "any",
) -> tuple[list[list[float]] | None, str | None]:
    """:func:`embed_texts`, and the name of the model that made the vectors (``None`` with no vectors).

    Every embedding, of a document or of a query, comes through here, so the
    guard is applied once for all of them.
    """
    _embedded_with.set(None)
    if not texts:
        return [], None
    vectors, model = _embed_guarded(texts, model, routing_tier)
    if vectors and model:
        _embedded_with.set((id(vectors), model))
    return vectors, model


def _embed_guarded(
    texts: list[str], model: str, routing_tier: str
) -> tuple[list[list[float]] | None, str | None]:
    # Prevent-at-source: sanitize + bound every text before any provider sees it.
    texts = [sanitize_for_embedding(t) for t in texts]

    # 1. Remote embedding server (e.g. nomic-embed-text on a remote host via VPN)
    remote = _remote_url()
    if remote and embedder_admitted(remote, routing_tier):
        result = _embed_remote(texts)
        if result is not None:
            return result, getenv("AXIOM_RAG_EMBED_MODEL", "nomic-embed-text")

    # 2. OpenAI (unless quota-blocked this session)
    if embedder_admitted(_OPENAI_API_URL, routing_tier):
        result = _embed_openai(texts, model)
        if result is not None:
            return result, model

    # 3. Local Ollama: this machine, admitted for every tier.
    result = _embed_ollama(texts)
    if result is not None:
        return result, _OLLAMA_EMBED_MODEL

    # Every provider returned None. Distinguish "a provider is configured but
    # failed" (transient — raise so the caller retries later) from "no provider
    # configured at all" (intentional — return None, caller falls back to keyword).
    if _provider_configured(routing_tier):
        # A persistent (input-rejection) error would already have been raised by
        # the provider below. Reaching here means every configured provider
        # failed transiently (unreachable / timeout / 5xx) — retryable.
        raise TransientEmbeddingError(
            "All configured embedding providers failed transiently "
            "(unreachable / timeout / 5xx) — retry once a provider recovers; "
            "the item is NOT indexed text-only.",
            provider="all",
        )

    refused = refused_embedders(routing_tier)
    if refused:
        log.warning(
            "Not embedded: %s may not receive %s text here, and this machine runs no "
            "embedder. RAG retrieval will fall back to keyword search.",
            " and ".join(refused), routing_tier,
        )
        return None, None
    log.warning(
        "No embedding provider available. Options:\n"
        "  • Set AXIOM_RAG_EMBED_URL to an OpenAI-compatible /v1/embeddings server\n"
        "  • Set OPENAI_API_KEY for OpenAI embeddings\n"
        "  • Run Ollama locally with nomic-embed-text\n"
        "RAG retrieval will fall back to keyword search."
    )
    return None, None


@dataclass(frozen=True)
class QueryEmbedding:
    """A retrieval query's vector, the model that made it, and what to tell the person.

    ``vector`` is ``None`` when the query is searched by keyword only; ``note``
    then says why when the reason is the guard (an embedder exists but is not
    cleared), and is empty when nothing is configured or a provider failed.
    """

    vector: list[float] | None
    model: str | None = None
    routing_tier: str = "any"
    note: str = ""
    #: Corpora whose recorded model is not the query's: searched by keyword only, while the
    #: others keep vector search (``retrieve(keyword_only_corpora=)``, ``search_with_fallback``).
    keyword_only: tuple[str, ...] = ()


def query_tier(text: str, routing_tier: str = "any") -> str:
    """The stricter of the session's label and the query's own keyword label.

    A surface with no conversation passes nothing and is held to what the
    query itself says; a chat session passes the tier it holds.
    """
    from axiom.llm.router import keyword_tier
    from axiom.llm.tier_policy import CONTROLLED

    if routing_tier == CONTROLLED:
        return CONTROLLED
    return CONTROLLED if keyword_tier(text) == CONTROLLED else (routing_tier or "any")


def embed_query(
    text: str,
    *,
    routing_tier: str = "any",
    store: object | None = None,
    corpora: list[str] | None = None,
    corpus_generation: int | None = None,
) -> QueryEmbedding:
    """Embed one retrieval query at the session's clearance level (RS-E2, P0.2).

    The one function every retrieval path embeds its query through: chat's
    ``_rag_context``, ``axiom_rag__retrieve``, ``search_docs``, ``axi rag
    search`` and ``axi rag eval``, memory recall, library search, a federated
    search and the ``/rag`` mount. Never raises: a provider that fails leaves
    the query to keyword search, as every one of those paths did before.

    With ``store``, the query's model is held to the model each of ``corpora``
    (``None``: every corpus) recorded for the generation it serves, or
    ``corpus_generation``. A vector from another model would rank a corpus's
    documents by noise, so that corpus alone is searched by keyword
    (``keyword_only``) and the note names it; the others keep the vector.
    """
    tier = query_tier(text, routing_tier)
    try:
        vectors, model = embed_texts_with_model([text], routing_tier=tier)
    except Exception as exc:  # noqa: BLE001 - retrieval degrades to keyword search
        log.debug("query embedding failed, searching by keyword: %s", exc)
        vectors, model = None, None
    if vectors:
        clashes = _declared_model_clashes(store, corpora, corpus_generation, model)
        if clashes:
            stale = tuple(c for c, _ in clashes)
            held = "; ".join(f"{c} holds vectors from {m}" for c, m in clashes)
            note = (
                f"Searched {', '.join(stale)} by keyword only: {held}, and this node embeds queries "
                f"with {model}. Rebuild {'it' if len(stale) == 1 else 'them'} as a new generation "
                f"with {model}, or embed queries with the recorded model."
            )
            # Every searched corpus mismatched: nothing may use the vector.
            everything = corpora is not None and set(stale) >= set(corpora)
            return QueryEmbedding(None if everything else vectors[0], model, tier, note, stale)
        return QueryEmbedding(vectors[0], model, tier)
    note = ""
    refused = refused_embedders(tier)
    if refused:
        why = (
            "this node's agent policy keeps model calls on this machine"
            if not _model_calls_may_leave()
            else f"{' and '.join(refused)} {'is' if len(refused) == 1 else 'are'} not cleared for {tier} content"
        )
        note = f"Searched by keyword only: {why}, and this machine runs no embedder."
    return QueryEmbedding(None, None, tier, note)


def _declared_model_clashes(
    store: object | None, corpora: list[str] | None, corpus_generation: int | None, model: str | None
) -> list[tuple[str, str]]:
    """``(corpus, recorded model)`` for every searched corpus that recorded a model other than ``model``.

    Empty when there is no store, the store keeps no records (a served remote
    store, a test double), or the records cannot be read.
    """
    declared_fn = getattr(store, "declared_embedding_models", None)
    if store is None or not model or not callable(declared_fn):
        return []
    try:
        declared = declared_fn(list(corpora) if corpora is not None else None, corpus_generation=corpus_generation)
    except Exception as exc:  # noqa: BLE001 - the check never costs the turn its retrieval
        log.debug("could not read declared embedding models: %s", exc)
        return []
    return [(c, held) for c, held in sorted((declared or {}).items()) if held and held != model]


#: The setting holding the cosine a re-embedded sample must reach to count as the same model.
VERIFY_COSINE_SETTING = "rag.embedding_verify_cosine"
_VERIFY_COSINE_DEFAULT = 0.999


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _verify_threshold(threshold: float | None) -> float:
    if threshold is not None:
        return float(threshold)
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore

        return float(SettingsStore().get(VERIFY_COSINE_SETTING, _VERIFY_COSINE_DEFAULT))
    except Exception:  # noqa: BLE001
        return _VERIFY_COSINE_DEFAULT


def verify_embedding_model(
    store,
    corpus: str,
    *,
    corpus_generation: int | None = None,
    sample: int = 20,
    threshold: float | None = None,
    apply: bool = False,
) -> dict:
    """Check which model made a corpus generation's stored vectors, and record it with ``apply``.

    For a generation indexed before records existed. A sample of stored chunks
    (``sample``, one per document) is embedded again with the configured
    embedder, each at its own label, and compared with the stored vector by
    cosine. Only when every one reaches ``threshold`` (the setting
    ``rag.embedding_verify_cosine``, default 0.999) is the model recorded, and
    only with ``apply``; otherwise nothing is written and the report says the
    corpus needs a re-embed. Recording is a reversible write: the row can be
    deleted.
    """
    generation = int(corpus_generation) if corpus_generation is not None else store.served_generation(corpus)
    report: dict = {"corpus": corpus, "generation": generation, "model": None, "sampled": 0, "documents": 0,
                    "min_cosine": None, "matched": False, "recorded": False, "message": ""}
    held = store.declared_embedding_models([corpus], corpus_generation=generation).get(corpus)
    if held:
        report.update(model=held, matched=True, message=f"{corpus} already records {held}; nothing to do")
        return report
    rows = store.sample_embedded_chunks(corpus, n=sample, corpus_generation=generation)
    report.update(sampled=len(rows), documents=len({r["source_path"] for r in rows}))
    if not rows:
        report["message"] = f"{corpus} generation {generation} holds no vectors; nothing to verify"
        return report
    from axiom.rag.ingest import _embed_tier

    cosines: list[float] = []
    models: set[str] = set()
    by_tier: dict[str, list[dict]] = {}
    for r in rows:
        by_tier.setdefault(_embed_tier(r.get("access_tier")), []).append(r)
    for tier, group in by_tier.items():
        vectors, model = embed_texts_with_model([r["chunk_text"] for r in group], routing_tier=tier)
        if not vectors or not model:
            report["message"] = (
                f"no embedder admitted for {tier} text is available here; nothing verified or recorded"
            )
            return report
        models.add(model)
        cosines += [_cosine(v, r["embedding"]) for v, r in zip(vectors, group, strict=True)]
    limit = _verify_threshold(threshold)
    report["min_cosine"] = round(min(cosines), 6)
    if len(models) != 1:
        report["message"] = f"the sample was embedded by more than one model ({', '.join(sorted(models))})"
        return report
    model = models.pop()
    report["model"] = model
    if min(cosines) < limit:
        below = sum(1 for c in cosines if c < limit)
        report["message"] = (
            f"{below} of {len(cosines)} sampled chunks differ from {model} (lowest cosine {min(cosines):.4f} "
            f"< {limit}): {corpus} was embedded by another model and needs a re-embed; nothing recorded"
        )
        return report
    report["matched"] = True
    if apply:
        recorded = store.record_embedding_model(corpus, generation, model)
        report["recorded"] = recorded == model
        report["message"] = f"recorded {recorded} for {corpus} generation {generation}"
    else:
        report["message"] = (
            f"every sampled chunk matches {model} (lowest cosine {min(cosines):.4f}); "
            "dry run: pass --apply to record it"
        )
    return report


def _provider_configured(routing_tier: str = "any") -> bool:
    """True if any embedding provider this tier may use is configured/reachable.

    Used to decide whether an all-None result means "failed" (configured) or
    "no provider" (not configured). Only consulted on the failure path, so the
    Ollama reachability probe is not on the hot path.
    """
    remote = _remote_url()
    if remote and embedder_admitted(remote, routing_tier):
        return True
    if (
        os.environ.get("OPENAI_API_KEY")
        and not _openai_quota_blocked
        and embedder_admitted(_OPENAI_API_URL, routing_tier)
    ):
        return True
    return _ollama_reachable()


def local_embedding_model() -> str | None:
    """The embedding model this machine itself serves (local Ollama), or ``None`` when it has none.

    The local step of the chain in :func:`embed_texts_with_model`, offered alone for a caller whose
    text may never go to another machine. It never pulls a model and never reads a remote setting.
    """
    if _ollama_reachable() and _ollama_has_model(_OLLAMA_EMBED_MODEL):
        return _OLLAMA_EMBED_MODEL
    return None


def local_embedding_dim() -> int:
    """The vector length of :func:`local_embedding_model` (the store's)."""
    return _STORE_DIMENSIONS


def embed_texts_local(texts: list[str]) -> list[list[float]] | None:
    """Embed on this machine only (the local Ollama step of the chain), or ``None``."""
    if not texts:
        return []
    return _embed_ollama([sanitize_for_embedding(t) for t in texts])


def provider_configured(routing_tier: str = "any") -> bool:
    """Whether any embedding provider ``routing_tier`` text may go to is configured or reachable."""
    return _provider_configured(routing_tier)


def _ollama_reachable() -> bool:
    """Cheap probe: is a local Ollama daemon responding?"""
    try:
        req = urllib.request.Request("http://localhost:11434/api/tags")
        with urllib.request.urlopen(req, timeout=2):
            return True
    except Exception:
        return False


def _embed_remote(texts: list[str]) -> list[list[float]] | None:
    """Embed via a configurable remote OpenAI-compatible endpoint.

    Reads:
      AXIOM_RAG_EMBED_URL    Base URL (e.g. https://<vpn-host>:42000)
      AXIOM_RAG_EMBED_MODEL  Model name (default: nomic-embed-text)
      AXIOM_RAG_EMBED_KEY    API key (optional)
    """
    base_url = getenv("AXIOM_RAG_EMBED_URL", "").rstrip("/")
    if not base_url:
        return None

    embed_model = getenv("AXIOM_RAG_EMBED_MODEL", "nomic-embed-text")
    api_key = getenv("AXIOM_RAG_EMBED_KEY", "")

    try:
        import requests as _requests
    except ImportError:
        return None

    url = f"{base_url}/v1/embeddings"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    all_embeddings: list[list[float]] = []

    for start in range(0, len(texts), _BATCH_SIZE):
        batch = texts[start: start + _BATCH_SIZE]
        payload = {"input": batch, "model": embed_model}

        for attempt in range(_MAX_RETRIES):
            try:
                resp = _requests.post(url, headers=headers, json=payload, timeout=30, verify=False)
            except Exception as e:
                log.debug("Remote embedding unreachable (%s): %s", base_url, e)
                return None

            if resp.status_code == 429:
                wait = _BACKOFF_BASE * (2 ** attempt)
                log.warning("Remote embedding rate limited, retrying in %.1fs", wait)
                time.sleep(wait)
                continue

            if resp.status_code in _PERSISTENT_STATUSES:
                # The input is bad (too long / invalid) — futile to retry or
                # fall back to another provider. Surface the real body + raise.
                raise PersistentEmbeddingError(
                    "remote embedder rejected input",
                    status=resp.status_code, body=resp.text, provider="remote")
            if resp.status_code >= 400:
                log.warning("Remote embedding transient error %d: %s",
                            resp.status_code, resp.text[:200])
                return None

            data = resp.json()
            sorted_data = sorted(data["data"], key=lambda d: d["index"])
            all_embeddings.extend(d["embedding"] for d in sorted_data)
            break
        else:
            return None

    return all_embeddings if all_embeddings else None


def _embed_openai(texts: list[str], model: str) -> list[list[float]] | None:
    """Embed via OpenAI API. Returns None if unavailable or quota-blocked."""
    global _openai_quota_blocked

    if _openai_quota_blocked:
        return None

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None

    try:
        import requests as _requests
    except ImportError:
        return None

    from axiom.infra.rate_limiter import get_limiter
    limiter = get_limiter("openai")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    all_embeddings: list[list[float]] = []

    for start in range(0, len(texts), _BATCH_SIZE):
        batch = texts[start: start + _BATCH_SIZE]
        payload: dict = {"input": batch, "model": model}
        if model.startswith("text-embedding-3"):
            payload["dimensions"] = _STORE_DIMENSIONS

        for attempt in range(_MAX_RETRIES):
            limiter.wait()
            resp = _requests.post(_OPENAI_API_URL, headers=headers, json=payload, timeout=60)
            limiter.update(resp)

            if resp.status_code == 429:
                # Check if it's a quota issue (not just rate limiting)
                body = resp.text.lower()
                if "exceeded your current quota" in body or "billing" in body:
                    log.warning("OpenAI quota exceeded — falling back to Ollama")
                    _openai_quota_blocked = True
                    return None

                wait = _BACKOFF_BASE * (2 ** attempt)
                log.warning("Rate limited by OpenAI, retrying in %.1fs", wait)
                time.sleep(wait)
                continue

            if resp.status_code in _PERSISTENT_STATUSES:
                raise PersistentEmbeddingError(
                    "OpenAI rejected input",
                    status=resp.status_code, body=resp.text, provider="openai")
            if resp.status_code >= 400:
                log.warning("OpenAI embeddings transient error %d: %s",
                            resp.status_code, resp.text[:200])
                return None

            data = resp.json()
            sorted_data = sorted(data["data"], key=lambda d: d["index"])
            all_embeddings.extend(d["embedding"] for d in sorted_data)
            break
        else:
            log.warning("OpenAI embeddings failed after %d retries", _MAX_RETRIES)
            return None

    return all_embeddings


def _embed_ollama(texts: list[str]) -> list[list[float]] | None:
    """Embed via local Ollama. Returns None if Ollama is not available."""
    # Ensure Ollama is running
    try:
        from axiom.infra.connections import ensure_available
        ensure_available("ollama")
    except Exception:
        pass

    # Check if embedding model is available, pull if needed
    if not _ollama_has_model(_OLLAMA_EMBED_MODEL):
        log.info("Pulling Ollama embedding model: %s", _OLLAMA_EMBED_MODEL)
        try:
            import subprocess
            subprocess.run(
                ["ollama", "pull", _OLLAMA_EMBED_MODEL],
                capture_output=True, timeout=300,
            )
        except Exception as e:
            log.warning("Could not pull Ollama model %s: %s", _OLLAMA_EMBED_MODEL, e)
            return None

    all_embeddings: list[list[float]] = []

    for text in texts:
        try:
            payload = json.dumps({
                "model": _OLLAMA_EMBED_MODEL,
                "input": text,
            }).encode()
            req = urllib.request.Request(
                _OLLAMA_API_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                embeddings = data.get("embeddings", [])
                if embeddings:
                    all_embeddings.append(embeddings[0])
                else:
                    return None
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")
            except Exception:
                pass
            if e.code in _PERSISTENT_STATUSES:
                # Bad input (e.g. content the embedder won't accept) — futile to
                # retry; surface the real status+body and let the caller quarantine.
                raise PersistentEmbeddingError(
                    "Ollama rejected input",
                    status=e.code, body=body, provider="ollama") from e
            log.warning("Ollama embedding transient %d: %s", e.code, body[:200])
            return None
        except Exception as e:
            log.warning("Ollama embedding unreachable/transient: %s", e)
            return None

    return all_embeddings if all_embeddings else None


def _ollama_has_model(model: str) -> bool:
    """Check if Ollama has a model pulled."""
    try:
        req = urllib.request.Request("http://localhost:11434/api/tags")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            models = [m.get("name", "") for m in data.get("models", [])]
            return any(model in m for m in models)
    except Exception:
        return False
