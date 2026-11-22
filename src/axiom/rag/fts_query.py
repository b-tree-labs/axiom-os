# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Full-text query construction for hybrid retrieval (driver-free).

Pure functions that turn a natural-language query into PostgreSQL ``tsquery``
strings. Shared by :mod:`axiom.rag.store` (the psycopg2 ``RAGStore``) and
:mod:`axiom.rag.hybrid` (the driver-agnostic shared retrieval module), so both
recall the same text candidates for the same query. Imports nothing but ``re``.

Two builders:

* :func:`fts_tsquery` -- broad OR-of-terms recall. ``websearch_to_tsquery``
  ANDs every term, so a multi-term question matched zero chunks and hybrid
  search silently collapsed to dense-only. OR plus ``ts_rank`` recalls
  broadly; a downstream reranker orders the pool.
* :func:`fts_entity_tsquery` -- precise recall that REQUIRES a named
  identifier (acronym, model number, short code) AND-ed with the OR of the
  remaining terms, so a bounded scan cannot drop the rare relevant chunk.
"""

from __future__ import annotations

import re

FTS_STOP: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "for",
        "and",
        "or",
        "to",
        "in",
        "on",
        "at",
        "is",
        "are",
        "was",
        "were",
        "be",
        "by",
        "with",
        "as",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "what",
        "which",
        "who",
        "whom",
        "how",
        "does",
        "do",
        "did",
        "either",
        "than",
        "then",
        "from",
        "into",
        "within",
        "between",
        "about",
        "also",
        "any",
        "all",
        "can",
        "could",
        "would",
        "should",
        "may",
        "might",
        "will",
        "shall",
        "not",
        "no",
        "but",
        "if",
        "so",
        "such",
        "we",
        "you",
        "our",
        "your",
        "there",
    }
)

# Short alphanumeric codes (A3, B12) the >=3-char word regex would otherwise
# drop. A site may supply an extra pattern for codes this one cannot see
# (hyphenated or all-letter abbreviations) via ``extra_code_re``.
FTS_CODE_RE = re.compile(r"[a-z]+[0-9]+|[0-9]+[a-z]+")
_WORD_RE = re.compile(r"[a-z0-9]{3,}")
_ENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]*")
_NON_LEX_RE = re.compile(r"[^a-z0-9]")


def fts_terms(text: str, *, extra_code_re: re.Pattern[str] | None = None) -> list[str]:
    """Content lexemes: short alphanumeric codes first, then words >= 3 chars,
    minus stopwords, sanitized to tsquery-safe tokens, order-preserving and
    deduplicated. ``extra_code_re`` (already compiled, matched against the
    lower-cased text) contributes additional code tokens ahead of the defaults."""
    low = (text or "").lower()
    toks: list[str] = []
    if extra_code_re is not None:
        toks.extend(m.group(0) for m in extra_code_re.finditer(low))
    toks.extend(FTS_CODE_RE.findall(low))
    toks.extend(_WORD_RE.findall(low))
    out: list[str] = []
    for tok in toks:
        lex = _NON_LEX_RE.sub("", tok)
        if len(lex) >= 2 and lex not in FTS_STOP and lex not in out:
            out.append(lex)
    return out


def fts_tsquery(text: str, *, extra_code_re: re.Pattern[str] | None = None) -> str:
    """OR-of-terms tsquery so a multi-term query recalls candidates.

    Returns "" when the query is all stopwords/punctuation (the caller then
    skips the text backend)."""
    return " | ".join(fts_terms(text, extra_code_re=extra_code_re))


def fts_entity_terms(text: str) -> list[str]:
    """Discriminative identifiers the query names: tokens carrying an uppercase
    letter or a digit in the ORIGINAL text (acronyms, model numbers, short
    codes). A broad OR query drowns their rare chunks under high-frequency
    generic terms and the bounded scan then drops them, so they are required
    in a separate precise ranking."""
    out: list[str] = []
    for tok in _ENTITY_RE.findall(text or ""):
        if any(ch.isupper() for ch in tok) or any(ch.isdigit() for ch in tok):
            lex = _NON_LEX_RE.sub("", tok.lower())
            if len(lex) >= 2 and lex not in FTS_STOP and lex not in out:
                out.append(lex)
    return out


def fts_entity_tsquery(text: str, *, extra_code_re: re.Pattern[str] | None = None) -> str:
    """Precise recall query: require a named identifier AND-ed with the OR of
    the remaining terms. Empty when the query names no identifier (the caller
    then runs only the broad OR backend). This shrinks the match set from the
    whole generic-term flood to the chunks that mention the named item, so a
    bounded scan cannot drop the rare relevant chunk."""
    ents = fts_entity_terms(text)
    if not ents:
        return ""
    rest = [t for t in fts_terms(text, extra_code_re=extra_code_re) if t not in ents]
    ent_or = " | ".join(ents)
    if not rest:
        return ent_or
    return f"({ent_or}) & ({' | '.join(rest)})"


def fts_entity_all_tsquery(text: str) -> str:
    """Require EVERY named identifier at once (``alpha & beta & gamma``).

    :func:`fts_entity_tsquery` ORs the identifiers, so a rare name is buried
    by the generic capitalised words around it; requiring all of them is
    precise. Empty unless the query names at least two (one is already
    required by :func:`fts_entity_tsquery`)."""
    ents = fts_entity_terms(text)
    return " & ".join(ents) if len(ents) >= 2 else ""


#: More names than this and requiring all of them would match nothing.
NAME_MAX_TERMS = 6
_HYPHENS_RE = re.compile(r"-+")


def fts_name_terms(text: str) -> list[str]:
    """Words that must ALL appear in a source's name (title + path) for it to match.

    The named identifiers of the query, with hyphenated codes split into their
    parts (``LAB-4658`` is ``lab`` and ``4658``, as Postgres parses a name).
    The query's first word is left out when only its first letter is a capital:
    that is the start of a sentence ("Summarize LAB-4658"), not a name, and
    requiring it matched nothing. Empty when the query names nothing, or more
    than :data:`NAME_MAX_TERMS`."""
    out: list[str] = []
    for i, m in enumerate(_ENTITY_RE.finditer(text or "")):
        tok = m.group(0)
        if not (any(ch.isupper() for ch in tok) or any(ch.isdigit() for ch in tok)):
            continue
        if i == 0 and tok[:1].isupper() and tok[1:].islower():
            continue
        for part in _HYPHENS_RE.split(tok.lower()):
            lex = _NON_LEX_RE.sub("", part)
            if len(lex) >= 2 and lex not in FTS_STOP and lex not in out:
                out.append(lex)
    return out if 1 <= len(out) <= NAME_MAX_TERMS else []


def fts_name_tsquery(text: str) -> str:
    """:func:`fts_name_terms` AND-ed together; empty when there are none."""
    return " & ".join(fts_name_terms(text))


__all__ = [
    "FTS_CODE_RE",
    "FTS_STOP",
    "NAME_MAX_TERMS",
    "fts_entity_all_tsquery",
    "fts_entity_terms",
    "fts_entity_tsquery",
    "fts_name_terms",
    "fts_name_tsquery",
    "fts_terms",
    "fts_tsquery",
]
