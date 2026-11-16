# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RAG chunk sanitizer — strips prompt injection patterns before LLM context insertion.

Loaded patterns come from runtime/config/injection_patterns.txt (facility-configurable).
The builtin default list is intentionally sparse; facilities describe their own
classified space in facility.toml and extend this list accordingly.

Usage:
    from axiom.rag.sanitizer import ChunkSanitizer

    sanitizer = ChunkSanitizer()
    clean_text, hits = sanitizer.sanitize(chunk_text)
    if hits:
        # hits is a list of matched pattern strings
        ...
"""

from __future__ import annotations

import logging
from pathlib import Path

from axiom import REPO_ROOT as _REPO_ROOT

_log = logging.getLogger(__name__)

_RUNTIME_CONFIG = _REPO_ROOT / "runtime" / "config"
_PATTERNS_FILE = _RUNTIME_CONFIG / "injection_patterns.txt"

# Example patterns ship with the package — locate relative to source tree
_SRC_ROOT = Path(__file__).resolve().parent.parent.parent.parent  # axiom/
_EXAMPLE_PATTERNS_FILE = (
    _SRC_ROOT / "runtime" / "config.example" / "injection_patterns.txt"
)

_REDACT_TOKEN = "[REDACTED:injection]"


def _load_patterns(path: Path) -> list[str]:
    """Load non-empty, non-comment lines from a pattern file."""
    if not path.exists():
        return []
    patterns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


class ChunkSanitizer:
    """Sanitizes retrieved RAG chunks to neutralize prompt injection attempts.

    Patterns are loaded lazily on first use and cached. Call reload() after
    the operator edits injection_patterns.txt to pick up changes without
    restarting.

    Matching is case-insensitive substring search. Matched spans are replaced
    with ``[REDACTED:injection]`` in the returned text.
    """

    def __init__(self, patterns_path: Path | None = None) -> None:
        self._path = Path(patterns_path) if patterns_path else _PATTERNS_FILE
        self._patterns: list[str] | None = None

    def _get_patterns(self) -> list[str]:
        if self._patterns is None:
            self.reload()
        return self._patterns  # type: ignore[return-value]

    def reload(self) -> None:
        """Force reload of pattern cache from disk."""
        patterns = _load_patterns(self._path)
        if not patterns:
            # Fall back to the example file (ships with the package)
            patterns = _load_patterns(_EXAMPLE_PATTERNS_FILE)
            if patterns:
                _log.debug(
                    "sanitizer: injection_patterns.txt not found — using example defaults "
                    "(copy runtime/config.example/injection_patterns.txt to runtime/config/)"
                )
        self._patterns = patterns
        _log.debug("sanitizer: loaded %d injection patterns", len(self._patterns))

    def sanitize(self, text: str) -> tuple[str, list[str]]:
        """Scan text for injection patterns and redact matches.

        Args:
            text: Raw chunk text from RAG retrieval.

        Returns:
            (sanitized_text, hits) where hits is a list of matched pattern
            strings (may be empty). sanitized_text has all matched spans
            replaced with ``[REDACTED:injection]``.
        """
        patterns = self._get_patterns()
        if not patterns:
            return text, []

        lower = text.lower()
        hits: list[str] = []
        offsets: list[tuple[int, int]] = []

        for pattern in patterns:
            p_lower = pattern.lower()
            start = 0
            while True:
                idx = lower.find(p_lower, start)
                if idx == -1:
                    break
                end = idx + len(p_lower)
                offsets.append((idx, end))
                if pattern not in hits:
                    hits.append(pattern)
                start = end

        if not offsets:
            return text, []

        # Merge overlapping spans, then rebuild the string
        offsets.sort()
        merged: list[tuple[int, int]] = []
        for start, end in offsets:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))

        parts: list[str] = []
        prev = 0
        for start, end in merged:
            parts.append(text[prev:start])
            parts.append(_REDACT_TOKEN)
            prev = end
        parts.append(text[prev:])

        return "".join(parts), hits


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_instance: ChunkSanitizer | None = None


def get_sanitizer() -> ChunkSanitizer:
    """Return the module-level ChunkSanitizer singleton."""
    global _instance
    if _instance is None:
        _instance = ChunkSanitizer()
    return _instance
