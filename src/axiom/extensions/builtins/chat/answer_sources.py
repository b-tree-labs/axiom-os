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
