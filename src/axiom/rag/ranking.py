# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What a site declares about how its documents rank (#1984).

A site knows things about its documents the platform cannot: which corpus is its own and which
is generic literature, which documents govern what it may do and which questions want them,
which words in a question ask for a value. The platform knows things that hold everywhere: a
design document or source file that reached a user-facing corpus is rarely the answer, and a
draft yields to its finished version. :class:`RankingProfile` carries both into the rerank of
:func:`axiom.rag.retriever.retrieve`, so every surface that retrieves (the chat, MCP retrieve,
``axi rag search``, a site's own chat) ranks a question the same way.

The node names its profile in ``AXIOM_RAG_RANKING`` (a TOML file, see :func:`load_profile`).
With none, :class:`RankingProfile` is the default, and the default changes nothing: every
document-kind weight is zero and no pattern is set, so ranking is what it was before profiles.

Stdlib only, so a serving process that carries no database driver can import it.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import tomllib
from dataclasses import dataclass, field, fields
from functools import cached_property
from pathlib import Path
from typing import Any

from axiom.rag.fts_query import fts_terms
from axiom.rag.hybrid import DEFAULT_VALUE_INTENT_TERMS, RerankWeights

log = logging.getLogger(__name__)

#: The environment variable naming the node's profile file.
PROFILE_ENV = "AXIOM_RAG_RANKING"

#: Developer files, by the naming software repositories use: kebab-case design documents
#: (``spec-``, ``adr-``, ``prd-``), code and build files, and code folders. Documents people
#: write for people are named in words ("Annual Report 2023.pdf") and do not match. Plain
#: alternation and classes only, so the same pattern means the same thing to Python's ``re``
#: and to Postgres's ``~`` (a name query can exclude developer files in SQL).
DEVELOPER_PATH_PATTERN = (
    r"(^|/)((spec|adr|prd)-[a-z0-9][a-z0-9-]*\.md"
    r"|CMakeLists\.txt|Makefile|Dockerfile"
    r"|[^/]+\.(py|c|cc|cpp|h|hpp|f90|jl|rs|go|js|ts|tsx|cmake|toml|yml|yaml|json|ipynb|sh))$"
    r"|(^|/)(packages|src|node_modules|\.github)/"
)

#: "draft" as a word of a file name or title (case-insensitive), not inside another word.
DRAFT_PATTERN = r"(^|[^a-z])draft([^a-z]|$)"

#: In a profile file, ``developer_paths = "default"`` and ``drafts = "default"`` mean the
#: patterns above; any other string is the site's own pattern.
_DEFAULT = "default"

_WEIGHT_NAMES = frozenset(f.name for f in fields(RerankWeights))
_SQL_IDENT = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$")


def _compile(pattern: str | None, name: str, flags: int = 0) -> re.Pattern[str] | None:
    if pattern is None:
        return None
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"{name} is not a valid regular expression: {exc}") from exc


@dataclass(frozen=True)
class RankingProfile:
    """A site's ranking declarations. Every field defaults to "changes nothing".

    ``authority_corpus`` / ``authority_path_re``: the site's own material, which gets the
    authority bonus (a developer file never does). ``value_intent_terms``: question words that
    ask for a value. ``extra_code_re``: code tokens the default lexer cannot see (hyphenated or
    all-letter abbreviations). ``developer_path_re`` / ``draft_re``: what counts as a developer
    file or a draft (:data:`DEVELOPER_PATH_PATTERN` and :data:`DRAFT_PATTERN` are the generic
    ones). ``primary_query_re`` / ``primary_document_re``: a question the site's governing
    documents answer, and those documents by path or title. Each document-kind signal moves a
    rank only by its weight in ``weights``.
    """

    weights: RerankWeights = field(default_factory=RerankWeights)
    authority_corpus: str | None = None
    authority_path_re: str | None = None
    value_intent_terms: tuple[str, ...] = DEFAULT_VALUE_INTENT_TERMS
    extra_code_re: str | None = None
    developer_path_re: str | None = None
    draft_re: str | None = None
    primary_query_re: str | None = None
    primary_document_re: str | None = None
    # -- recall (#1984 step 1b): each off until the site turns it on --------------------------
    entity_rankings: bool = False
    """Two more text rankings: one requiring any identifier the question names, one requiring
    all of them, so a rare code is not buried under the generic terms around it."""
    name_ranking: bool = False
    """Rank the sources whose name (title and path) carries the question's names, and give the
    best of their chunks reserved places at the front."""
    name_max_sources: int = 6
    """More sources than this match the name and it is a topic, not a document: stand down."""
    name_chunks: int = 12
    name_reserved: int = 2
    primary_name_tsquery: str | None = None
    """For a question ``primary_query_re`` matches, the name query of the governing documents
    (a tsquery), which may match up to ``primary_name_max_sources`` sources (copies kept in
    other folders)."""
    primary_name_max_sources: int = 12
    names_table: str | None = None
    """A table of per-source names (``corpus``, ``source_path``, ``name_tsv``) the site keeps
    indexed; ``None`` reads titles and paths from the store's ``documents`` table."""
    dedup_prefix_chars: int = 0
    """Chunks sharing this many leading characters are one passage; 0 keeps every chunk."""
    retrieval_breadth: int | None = None
    """Candidates each ranking returns before fusion; ``None`` is the caller's."""

    def __post_init__(self) -> None:
        for name in ("authority_path_re", "extra_code_re", "developer_path_re", "draft_re",
                     "primary_query_re", "primary_document_re"):
            _compile(getattr(self, name), name)
        if self.names_table is not None and not _SQL_IDENT.match(self.names_table):
            raise ValueError(f"names_table is not a plain table name: {self.names_table!r}")
        for name in ("name_max_sources", "name_chunks", "primary_name_max_sources"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")
        if self.name_reserved < 0 or self.dedup_prefix_chars < 0:
            raise ValueError("name_reserved and dedup_prefix_chars must not be negative")
        if self.retrieval_breadth is not None and self.retrieval_breadth < 1:
            raise ValueError("retrieval_breadth must be at least 1")

    # -- compiled once per profile --------------------------------------------------------

    @cached_property
    def authority_pattern(self) -> re.Pattern[str] | None:
        return _compile(self.authority_path_re, "authority_path_re")

    @cached_property
    def extra_code_pattern(self) -> re.Pattern[str] | None:
        return _compile(self.extra_code_re, "extra_code_re")

    @cached_property
    def _developer(self) -> re.Pattern[str] | None:
        return _compile(self.developer_path_re, "developer_path_re")

    @cached_property
    def _draft(self) -> re.Pattern[str] | None:
        return _compile(self.draft_re, "draft_re", re.I)

    @cached_property
    def _primary_query(self) -> re.Pattern[str] | None:
        return _compile(self.primary_query_re, "primary_query_re", re.I)

    @cached_property
    def _primary_document(self) -> re.Pattern[str] | None:
        return _compile(self.primary_document_re, "primary_document_re", re.I)

    # -- the signals ----------------------------------------------------------------------

    def terms(self, text: str) -> list[str]:
        """Content terms of ``text``, with the site's extra code tokens."""
        return fts_terms(text, extra_code_re=self.extra_code_pattern)

    def is_developer_file(self, path: str | None) -> bool:
        return bool(self._developer and self._developer.search(path or ""))

    def is_draft(self, path: str | None, title: str | None) -> bool:
        if self._draft is None:
            return False
        name = (path or "").replace("\\", "/").rsplit("/", 1)[-1]
        return bool(self._draft.search(name) or self._draft.search(title or ""))

    def wants_primary_document(self, query: str | None) -> bool:
        return bool(self._primary_query and self._primary_query.search(query or ""))

    def name_query(self, query: str | None) -> tuple[str, int]:
        """The name tsquery for ``query`` and how many sources it may match ("" when none)."""
        from axiom.rag.fts_query import fts_name_tsquery

        if self.primary_name_tsquery and self.wants_primary_document(query):
            return self.primary_name_tsquery, self.primary_name_max_sources
        return fts_name_tsquery(query or ""), self.name_max_sources

    def is_primary_document(self, path: str | None, title: str | None) -> bool:
        """One of the site's governing documents, by path or title; never a developer file,
        whatever its title says."""
        if self._primary_document is None or self.is_developer_file(path):
            return False
        return bool(self._primary_document.search(path or "") or self._primary_document.search(title or ""))


# ---------------------------------------------------------------------------
# The profile file
# ---------------------------------------------------------------------------

_KEYS = frozenset({
    "weights", "authority_corpus", "authority_path_re", "value_intent_terms", "extra_code_re",
    "developer_paths", "drafts", "primary_query_re", "primary_document_re",
    "entity_rankings", "name_ranking", "name_max_sources", "name_chunks", "name_reserved",
    "primary_name_tsquery", "primary_name_max_sources", "names_table", "dedup_prefix_chars",
    "retrieval_breadth",
})
_BOOLS = ("entity_rankings", "name_ranking")
_INTS = ("name_max_sources", "name_chunks", "name_reserved", "primary_name_max_sources",
         "dedup_prefix_chars", "retrieval_breadth")


def profile_from_dict(raw: dict[str, Any]) -> RankingProfile:
    """A profile from the ``[ranking]`` table of a profile file. Refuses unknown keys, so a
    misspelt key is an error rather than a setting that silently does nothing."""
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        raise ValueError(f"unknown ranking keys: {', '.join(unknown)} (known: {', '.join(sorted(_KEYS))})")
    weights_raw = raw.get("weights", {})
    if not isinstance(weights_raw, dict):
        raise ValueError("[ranking.weights] must be a table of weights")
    bad = sorted(set(weights_raw) - _WEIGHT_NAMES)
    if bad:
        raise ValueError(f"unknown ranking weights: {', '.join(bad)} (known: {', '.join(sorted(_WEIGHT_NAMES))})")
    try:
        weights = RerankWeights(**{k: float(v) for k, v in weights_raw.items()})
    except (TypeError, ValueError) as exc:
        raise ValueError(f"[ranking.weights] must be numbers: {exc}") from exc

    def pattern(key: str, default: str) -> str | None:
        value = raw.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        return default if value == _DEFAULT else value

    kwargs: dict[str, Any] = {
        "weights": weights,
        "developer_path_re": pattern("developer_paths", DEVELOPER_PATH_PATTERN),
        "draft_re": pattern("drafts", DRAFT_PATTERN),
    }
    for key in _BOOLS:
        if key in raw:
            if not isinstance(raw[key], bool):
                raise ValueError(f"{key} must be true or false")
            kwargs[key] = raw[key]
    for key in _INTS:
        if key in raw:
            if not isinstance(raw[key], int) or isinstance(raw[key], bool):
                raise ValueError(f"{key} must be a whole number")
            kwargs[key] = raw[key]
    for key in ("authority_corpus", "authority_path_re", "extra_code_re", "primary_query_re",
                "primary_document_re", "primary_name_tsquery", "names_table"):
        if key in raw:
            if not isinstance(raw[key], str):
                raise ValueError(f"{key} must be a string")
            kwargs[key] = raw[key]
    if "value_intent_terms" in raw:
        terms = raw["value_intent_terms"]
        if not isinstance(terms, list) or not all(isinstance(t, str) for t in terms):
            raise ValueError("value_intent_terms must be a list of words")
        kwargs["value_intent_terms"] = tuple(t.lower() for t in terms)
    return RankingProfile(**kwargs)


def load_profile(path: str | Path) -> RankingProfile:
    """Read a profile file: a ``[ranking]`` table, and ``[ranking.weights]``. Raises
    ``ValueError`` for anything it cannot use. Example::

        [ranking]
        authority_corpus = "site-docs"
        value_intent_terms = ["worth", "value", "limit", "cost"]
        developer_paths = "default"
        drafts = "default"
        primary_query_re = "\\\\b(charter|licen[sc]ed?)\\\\b"
        primary_document_re = "charter|operating licen[sc]e"

        [ranking.weights]
        developer = 1.0
        draft = 0.75
        primary = 2.0
    """
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read ranking profile {path}: {exc}") from exc
    raw = data.get("ranking", {})
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: [ranking] must be a table")
    return profile_from_dict(raw)


_lock = threading.Lock()
_cache: dict[str, tuple[float, RankingProfile]] = {}


def active_profile() -> RankingProfile:
    """The node's profile: the file ``AXIOM_RAG_RANKING`` names, read again when it changes.

    No file named means the default. A file that cannot be used is logged and the default is
    used: retrieval with the default ranking still answers, where a raised error would cost the
    turn every passage."""
    path = os.environ.get(PROFILE_ENV, "").strip()
    if not path:
        return RankingProfile()
    try:
        mtime = os.stat(path).st_mtime
    except OSError as exc:
        log.warning("%s=%s cannot be read (%s); ranking with the default profile", PROFILE_ENV, path, exc)
        return RankingProfile()
    with _lock:
        hit = _cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        profile = load_profile(path)
    except ValueError as exc:
        log.warning("%s=%s is not a usable ranking profile (%s); ranking with the default profile",
                    PROFILE_ENV, path, exc)
        return RankingProfile()
    with _lock:
        _cache[path] = (mtime, profile)
    return profile


__all__ = [
    "DEVELOPER_PATH_PATTERN",
    "DRAFT_PATTERN",
    "PROFILE_ENV",
    "RankingProfile",
    "active_profile",
    "load_profile",
    "profile_from_dict",
]
