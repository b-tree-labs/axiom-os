# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Embedding generation with automatic provider fallback.

Provider chain:
1. Remote embed endpoint (NEUT_EMBED_URL env var) — any OpenAI-compatible /v1/embeddings server
   e.g. a nomic-embed-text llama.cpp instance on a remote VPN host
2. OpenAI API (text-embedding-3-small) — if OPENAI_API_KEY set and quota available
3. Ollama local (nomic-embed-text) — free, offline, no API key needed
4. None — caller handles fallback (e.g., keyword search)

Environment variables:
  NEUT_EMBED_URL    Base URL of an OpenAI-compatible embedding server
                    e.g. https://<vpn-host>:42000
  NEUT_EMBED_MODEL  Model name to request (default: nomic-embed-text)
  NEUT_EMBED_KEY    API key for the remote endpoint (optional)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

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
_OLLAMA_API_URL = "http://localhost:11434/api/embed"
_OLLAMA_EMBED_MODEL = "nomic-embed-text"

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


def embed_texts(
    texts: list[str],
    model: str = "text-embedding-3-small",
) -> list[list[float]] | None:
    """Embed texts using the best available provider.

    Priority: remote NEUT_EMBED_URL → OpenAI → local Ollama → None.
    Returns None if no provider is available (caller falls back to keyword search).
    """
    if not texts:
        return []

    # Prevent-at-source: sanitize + bound every text before any provider sees it.
    texts = [sanitize_for_embedding(t) for t in texts]

    # 1. Remote embedding server (e.g. nomic-embed-text on a remote host via VPN)
    result = _embed_remote(texts)
    if result is not None:
        return result

    # 2. OpenAI (unless quota-blocked this session)
    result = _embed_openai(texts, model)
    if result is not None:
        return result

    # 3. Local Ollama
    result = _embed_ollama(texts)
    if result is not None:
        return result

    # Every provider returned None. Distinguish "a provider is configured but
    # failed" (transient — raise so the caller retries later) from "no provider
    # configured at all" (intentional — return None, caller falls back to keyword).
    if _provider_configured():
        # A persistent (input-rejection) error would already have been raised by
        # the provider below. Reaching here means every configured provider
        # failed transiently (unreachable / timeout / 5xx) — retryable.
        raise TransientEmbeddingError(
            "All configured embedding providers failed transiently "
            "(unreachable / timeout / 5xx) — retry once a provider recovers; "
            "the item is NOT indexed text-only.",
            provider="all",
        )

    log.warning(
        "No embedding provider available. Options:\n"
        "  • Set NEUT_EMBED_URL to an OpenAI-compatible /v1/embeddings server\n"
        "  • Set OPENAI_API_KEY for OpenAI embeddings\n"
        "  • Run Ollama locally with nomic-embed-text\n"
        "RAG retrieval will fall back to keyword search."
    )
    return None


def _provider_configured() -> bool:
    """True if any embedding provider is configured/reachable.

    Used to decide whether an all-None result means "failed" (configured) or
    "no provider" (not configured). Only consulted on the failure path, so the
    Ollama reachability probe is not on the hot path.
    """
    if os.environ.get("NEUT_EMBED_URL"):
        return True
    if os.environ.get("OPENAI_API_KEY") and not _openai_quota_blocked:
        return True
    return _ollama_reachable()


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
      NEUT_EMBED_URL    Base URL (e.g. https://<vpn-host>:42000)
      NEUT_EMBED_MODEL  Model name (default: nomic-embed-text)
      NEUT_EMBED_KEY    API key (optional)
    """
    base_url = os.environ.get("NEUT_EMBED_URL", "").rstrip("/")
    if not base_url:
        return None

    embed_model = os.environ.get("NEUT_EMBED_MODEL", "nomic-embed-text")
    api_key = os.environ.get("NEUT_EMBED_KEY", "")

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
        payload = {"input": batch, "model": model}

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
