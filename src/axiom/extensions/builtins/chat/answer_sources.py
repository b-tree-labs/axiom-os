# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Which sources an answer cited, remembered per answer (ADR-168).

Retrieval keys (``C1``, ``C2``...) are only meaningful inside the ``retrieve()`` call that produced them,
and a session stores messages, not chunks. So once the next turn runs, ``[C2]`` in an old answer resolves
to nothing. This module keeps the small, durable part: for each answer, the sources it cited by marker,
keyed by the answer's own timestamp, inside ``session.context`` (which every session store already
persists). ``/compact`` reads it so that summarising a grounded conversation does not lose its provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

KEY = "answer_sources"


def record(session: Any, retrieved: list | None) -> None:
    """Remember the sources the session's latest answer cited. No-ops for an ungrounded or uncited answer."""
    messages = getattr(session, "messages", None) or []
    if not retrieved or not messages or messages[-1].role != "assistant":
        return
    from axiom.rag.citation import postprocess_citations

    last = messages[-1]
    cited = postprocess_citations(last.content or "", retrieved).cited
    if not cited:
        return
    session.context.setdefault(KEY, {})[last.timestamp] = [
        {"key": c.citation_key, "path": c.source_path, "title": c.source_title, "corpus": c.corpus}
        for c in cited
    ]


def for_message(session: Any, message: Any) -> list[dict[str, str]]:
    """The sources recorded for ``message``, or an empty list."""
    return list((getattr(session, "context", None) or {}).get(KEY, {}).get(message.timestamp, []))


# ---- which sources an answer shows -----------------------------------------------------------

#: The corpus a data read is listed under, so a surface can tell a read from a document.
DATA_CORPUS = "data"

#: Argument names that say what a read read, in the order a reader wants them.
_READ_FIELDS = (
    "site", "feed", "channel", "channels", "start", "end", "since", "until", "window",
    "last", "bucket", "agg", "aggregate", "tier",
)


@dataclass(frozen=True)
class DataReadSource:
    """One data read an answer made, shaped like a retrieved chunk so every renderer lists it."""

    citation_key: str
    source_title: str
    source_path: str
    rank: int
    corpus: str = DATA_CORPUS
    chunk_index: int = 0


def _describe_read(tool: str, args: dict[str, Any]) -> str:
    parts = [tool]
    for name in _READ_FIELDS:
        value = args.get(name)
        if value in (None, "", [], ()):
            continue
        if isinstance(value, (list, tuple)):
            value = ",".join(str(v) for v in value)
        parts.append(f"{name}={value}")
    return " · ".join(parts)


def data_read_sources(reads: list[dict[str, Any]], served: list[str]) -> list[DataReadSource]:
    """The data reads of a turn as sources: the client's own calls, then the verbs the node ran.

    Identical reads are listed once. Keys are ``D1``, ``D2``... so they cannot be mistaken for a
    document marker, and ranks are below every document's so the reads lead the list.
    """
    described: list[tuple[str, str]] = []
    seen: set[str] = set()
    for r in reads:
        tool = str(r.get("tool") or "")
        if not tool:
            continue
        args = r.get("args") if isinstance(r.get("args"), dict) else {}
        title = _describe_read(tool, args)
        if title not in seen:
            seen.add(title)
            described.append((title, f"data:{title}"))
    for verb in served:
        title = f"{verb} (run on the node)"
        if verb and title not in seen:
            seen.add(title)
            described.append((title, f"data:node:{verb}"))
    n = len(described)
    return [
        DataReadSource(citation_key=f"D{i + 1}", source_title=t, source_path=p, rank=i - n)
        for i, (t, p) in enumerate(described)
    ]


def sources_for_answer(
    answer: str,
    retrieved: list | None,
    *,
    reads: list[dict[str, Any]] | None = None,
    served: list[str] | None = None,
) -> list:
    """What an answer's Sources list shows: what the answer used, and nothing else.

    * No data read this turn: the retrieved chunks, as before. Retrieval was the only thing
      that could ground the answer.
    * A data read this turn: the reads, plus only the documents the answer cites by marker.
      A retriever runs on every question; a chunk it surfaced that the answer never used did
      not ground a number read from the data, and listing it says it did.
    """
    retrieved = list(retrieved or [])
    data = data_read_sources(list(reads or []), list(served or []))
    if not data:
        return retrieved
    if not retrieved:
        return list(data)
    from axiom.rag.citation import postprocess_citations

    cited = {c.citation_key for c in postprocess_citations(answer or "", retrieved).cited}
    return [*data, *(c for c in retrieved if getattr(c, "citation_key", "") in cited)]
