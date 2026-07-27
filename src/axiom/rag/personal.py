# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Personal corpus ingestion — sources beyond static docs.

Extracts indexable text from:
  - Chat session transcripts (runtime/sessions/*.json)
  - Processed sense signals  (runtime/inbox/processed/*.json)
  - Git commit logs          (git repos found under runtime/knowledge/)

Each source type is stored with a synthetic source_path that keeps it
isolated from file-based documents (sessions/, signals/, git-log/ prefixes).
Checksum-based deduplication means re-runs only re-index changed files.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from pathlib import Path

from .chunker import chunk_markdown
from .embeddings import embed_texts
from .store import CORPUS_INTERNAL, RAGStore

log = logging.getLogger(__name__)

# Minimum message count for a session to be worth indexing
_MIN_SESSION_MESSAGES = 3


def _md5_text(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def _upsert(
    source_path: str,
    source_title: str,
    source_type: str,
    text: str,
    store: RAGStore,
    corpus: str,
) -> bool:
    """Chunk *text* and upsert into store. Returns True if indexed (not skipped)."""
    checksum = _md5_text(text)
    existing = store.get_document(source_path, corpus=corpus)
    if existing and existing.get("checksum") == checksum:
        log.debug("Unchanged, skipping: %s", source_path)
        return False

    chunks = chunk_markdown(text, source_path)
    if not chunks:
        return False

    # Override source metadata on each chunk
    for c in chunks:
        c.source_title = source_title
        c.source_type = source_type

    texts = [c.text for c in chunks]
    try:
        embeddings = embed_texts(texts)
    except Exception as exc:
        # A configured embedder failed (e.g. network drop). Don't commit text-only
        # with a checksum — that would skip the doc as 'unchanged' on re-run. Skip
        # now so a later run retries it once the provider recovers.
        log.warning(
            "Embedding failed for %s — not indexed, will retry on re-run: %s", source_path, exc
        )
        return False

    store.upsert_chunks(chunks, embeddings, checksum=checksum, corpus=corpus)
    log.info("Indexed %s (%d chunks)", source_path, len(chunks))
    return True


# ---------------------------------------------------------------------------
# Session transcripts
# ---------------------------------------------------------------------------

def _extract_session_text(session_path: Path) -> tuple[str, str] | None:
    """Return (title, markdown_text) for a session file, or None if too small."""
    try:
        data = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Cannot read session %s: %s", session_path, exc)
        return None

    title = data.get("title") or f"Session {session_path.stem}"
    messages = data.get("messages", [])

    parts: list[str] = [f"# {title}"]
    for msg in messages:
        role = msg.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content", "") or ""
        if isinstance(content, list):
            # Anthropic-style content blocks
            content = " ".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )
        content = content.strip()
        if content:
            label = "User" if role == "user" else "Assistant"
            parts.append(f"**{label}:** {content}")

    # Skip tiny sessions — not enough signal
    real_turns = sum(1 for m in messages if m.get("role") in ("user", "assistant"))
    if real_turns < _MIN_SESSION_MESSAGES:
        return None

    return title, "\n\n".join(parts)


def ingest_session_file(
    session_path: Path,
    store: RAGStore,
    corpus: str = CORPUS_INTERNAL,
) -> bool:
    """Ingest one session JSON file. Returns True if indexed."""
    result = _extract_session_text(session_path)
    if result is None:
        return False
    title, text = result
    source_path = f"sessions/{session_path.name}"
    return _upsert(source_path, title, "session", text, store, corpus)


def ingest_sessions(
    sessions_dir: Path,
    store: RAGStore,
    corpus: str = CORPUS_INTERNAL,
) -> tuple[int, int]:
    """Ingest all session JSON files. Returns (indexed, skipped)."""
    indexed = skipped = 0
    for p in sorted(sessions_dir.glob("*.json")):
        if ingest_session_file(p, store, corpus):
            indexed += 1
        else:
            skipped += 1
    log.info("Sessions: %d indexed, %d skipped", indexed, skipped)
    return indexed, skipped


# ---------------------------------------------------------------------------
# Processed sense signals
# ---------------------------------------------------------------------------

def _flatten_json(obj, prefix: str = "", max_depth: int = 4) -> list[str]:
    """Recursively extract string values from a JSON object as readable lines."""
    lines: list[str] = []
    if max_depth <= 0:
        return lines
    if isinstance(obj, dict):
        for k, v in obj.items():
            child_prefix = f"{prefix}.{k}" if prefix else k
            lines.extend(_flatten_json(v, child_prefix, max_depth - 1))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50]):  # cap list items
            lines.extend(_flatten_json(v, f"{prefix}[{i}]", max_depth - 1))
    elif isinstance(obj, str) and obj.strip():
        lines.append(f"- **{prefix}**: {obj.strip()}")
    elif isinstance(obj, (int, float, bool)):
        lines.append(f"- **{prefix}**: {obj}")
    return lines


def _extract_signal_text(signal_path: Path) -> tuple[str, str] | None:
    """Return (title, markdown_text) from a processed signal JSON file."""
    try:
        raw = signal_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Cannot read signal file %s: %s", signal_path, exc)
        return None

    title = f"Processed Signals: {signal_path.stem}"
    lines = _flatten_json(data)
    if not lines:
        return None

    text = f"# {title}\n\n" + "\n".join(lines)
    return title, text


def ingest_signals(
    inbox_dir: Path,
    store: RAGStore,
    corpus: str = CORPUS_INTERNAL,
) -> tuple[int, int]:
    """Ingest all processed signal JSON files. Returns (indexed, skipped)."""
    indexed = skipped = 0
    for p in sorted(inbox_dir.rglob("*.json")):
        result = _extract_signal_text(p)
        if result is None:
            skipped += 1
            continue
        title, text = result
        source_path = f"signals/{p.name}"
        if _upsert(source_path, title, "signal", text, store, corpus):
            indexed += 1
        else:
            skipped += 1
    log.info("Signals: %d indexed, %d skipped", indexed, skipped)
    return indexed, skipped


# ---------------------------------------------------------------------------
# Git commit logs
# ---------------------------------------------------------------------------

def _git_log_text(repo_dir: Path, max_commits: int = 300) -> str | None:
    """Return formatted git log markdown for a repo, or None on failure."""
    from axiom.infra.git import safe_git_env
    try:
        result = subprocess.run(
            [
                "git", "-C", str(repo_dir),
                "log",
                "--format=### %h %as%n%s%n%n%b",
                f"-n{max_commits}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=safe_git_env(repo_dir),
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def ingest_git_logs(
    knowledge_dir: Path,
    store: RAGStore,
    corpus: str = CORPUS_INTERNAL,
) -> tuple[int, int]:
    """Find git repos under knowledge_dir and index their commit logs.

    Returns (indexed, skipped).
    """
    indexed = skipped = 0

    # Find all .git directories (immediate children only — don't recurse into submodules)
    git_repos = [p.parent for p in knowledge_dir.rglob(".git") if p.is_dir()]

    for repo_dir in sorted(git_repos):
        repo_name = repo_dir.name
        log_text = _git_log_text(repo_dir)
        if not log_text:
            skipped += 1
            continue

        title = f"Git Log: {repo_name}"
        text = f"# {title}\n\nCommit history for repository `{repo_name}`.\n\n{log_text}"
        source_path = f"git-log/{repo_name}.md"

        if _upsert(source_path, title, "git-log", text, store, corpus):
            indexed += 1
        else:
            skipped += 1

    log.info("Git logs: %d indexed, %d skipped", indexed, skipped)
    return indexed, skipped
