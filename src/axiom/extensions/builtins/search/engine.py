# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One search over what a node holds, bounded by what the asker may read.

Modelled on the reference product's global search (SoilMetrix
``search_service``): a fast navigable **suggest** for the palette, and a
fuller **full** search behind it. Both federate the same providers and return
the same row shape, so the web palette, ``axi search``, ``neut chat`` and the
MCP tool all see one answer.

Providers, and the bound each applies:

- **conversations**: title AND message text, through the chat store's own
  site and ownership clause (a conversation the asker may not read does not
  match, and is not hinted at).
- **logbooks**: signed record text, for the sites this node serves.
- **corpus** (full only): the RAG corpus through ``axiom_rag__retrieve``, the
  platform primitive with the fail-closed access filter. Search never
  re-implements access control; it calls the seam that already has it.

A provider that is not configured here (no chat store, no logbooks, no RAG
store) contributes nothing and says so in ``skipped`` rather than failing the
search — the same contract the MCP primitive keeps.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: Modes. ``suggest`` is the palette's per-keystroke call: fast providers
#: only, few rows. ``full`` is the results page and the CLI.
SUGGEST = "suggest"
FULL = "full"

KINDS = ("conversation", "logbook", "corpus")


@dataclass(frozen=True)
class Hit:
    """One result. ``url`` is an app route a browser can open, when one exists."""

    kind: str
    id: str
    title: str
    snippet: str = ""
    url: str = ""
    score: float = 0.0
    site: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Scope:
    """Who is asking, and over what. ``sites`` None means unbounded (a dev box)."""

    principal: str = ""
    sites: frozenset[str] | None = None
    include_archived: bool = False


def _score(query: str, title: str, text: str) -> float:
    """Title hits outrank body hits; an exact title outranks a partial one."""
    q, t = query.lower(), (title or "").lower()
    if t == q:
        return 1.0
    if q in t:
        return 0.9 if t.startswith(q) else 0.8
    return 0.6 if q in (text or "").lower() else 0.4


# -- providers --------------------------------------------------------------


def conversations(query: str, scope: Scope, limit: int) -> list[Hit]:
    # The chat API's own backend selection — configured database, else the
    # zero-config local store — so search reads what the chat surface wrote.
    from axiom.infra.orchestrator.conversation_store import resolve_conversation_store

    store = resolve_conversation_store(None)
    if not hasattr(store, "search_conversations"):
        raise _Skip("this conversation store cannot search message text")
    site = next(iter(scope.sites)) if scope.sites and len(scope.sites) == 1 else None
    rows = store.search_conversations(
        query,
        site_id=site,
        principal=scope.principal,
        include_archived=scope.include_archived,
        limit=limit,
    )
    hits = []
    for r in rows:
        if scope.sites is not None and r.get("site_id") and r["site_id"] not in scope.sites:
            continue
        title = r.get("title") or "Untitled chat"
        hits.append(
            Hit(
                kind="conversation",
                id=str(r["id"]),
                title=title,
                snippet=r.get("snippet") or "",
                url=f"#/chat/{r['id']}",
                score=_score(query, title, r.get("snippet") or ""),
                site=r.get("site_id") or "",
                meta={"matched": r.get("matched"), "archived": r.get("archived", False)},
            )
        )
    return hits


def logbooks(query: str, scope: Scope, limit: int) -> list[Hit]:
    from axiom.extensions.builtins.attest import registry, service
    from axiom.infra.site_scope import deployment_sites

    sites = scope.sites if scope.sites is not None else deployment_sites()
    if not sites:
        return []
    q = query.lower()
    hits: list[Hit] = []
    for book in registry.all_logbooks():
        for site in sorted(sites):
            for rec in reversed(service.records(site, book.id)):
                content = rec.get("content") or {}
                text = json.dumps(content, ensure_ascii=False, default=str)
                if q not in text.lower():
                    continue
                brief = service.brief(rec)
                title = brief.get("title") or f"{book.display} #{brief['seq']}"
                hits.append(
                    Hit(
                        kind="logbook",
                        id=str(brief["attestation_id"]),
                        title=str(title),
                        snippet=_window(text, query),
                        url=f"#/log/{book.id}/{brief['attestation_id']}",
                        score=_score(query, str(title), text),
                        site=site,
                        meta={"logbook": book.id, "seq": brief["seq"], "signer": brief["signer"]},
                    )
                )
                if len(hits) >= limit:
                    return hits
    return hits


def corpus(query: str, scope: Scope, limit: int) -> list[Hit]:
    from axiom.extensions.builtins.mcp.platform_primitives import _HANDLERS

    result = asyncio.run(_HANDLERS["axiom_rag__retrieve"]({"query": query, "k": limit}))
    if not result.get("ok", True) and not result.get("results"):
        raise _Skip(result.get("note") or result.get("error") or "corpus unavailable")
    hits = []
    for i, r in enumerate(result.get("results") or []):
        title = r.get("source_title") or r.get("source_path") or "Document"
        hits.append(
            Hit(
                kind="corpus",
                id=str(r.get("source_path") or i),
                title=str(title),
                snippet=_window(r.get("text") or "", query),
                score=0.5 - i * 0.01,
                meta={"path": r.get("source_path")},
            )
        )
    return hits


class _Skip(Exception):
    """A provider is not configured here — reported, not raised."""


def _window(text: str, needle: str, width: int = 160) -> str:
    flat = " ".join(str(text or "").split())
    at = flat.lower().find(needle.lower())
    start = max(0, at - width // 3) if at >= 0 else 0
    end = min(len(flat), start + width)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


PROVIDERS: dict[str, Callable[[str, Scope, int], list[Hit]]] = {
    "conversation": conversations,
    "logbook": logbooks,
    "corpus": corpus,
}

#: The palette is per keystroke: the corpus (embedding + vector search) is not.
SUGGEST_KINDS = ("conversation", "logbook")


def search(
    query: str,
    scope: Scope,
    *,
    mode: str = FULL,
    kinds: list[str] | None = None,
    limit: int = 20,
    providers: dict[str, Callable[[str, Scope, int], list[Hit]]] | None = None,
) -> dict[str, Any]:
    """Run the query across providers; merge, rank, cap.

    Returns ``{query, mode, results, skipped}``. ``skipped`` maps a provider to
    why it contributed nothing, so a caller can say "logbooks are not served
    on this node" rather than implying there were no matches.
    """
    q = " ".join((query or "").split())
    table = providers or PROVIDERS
    wanted = [k for k in (kinds or (SUGGEST_KINDS if mode == SUGGEST else KINDS)) if k in table]
    out: list[Hit] = []
    skipped: dict[str, str] = {}
    if len(q) < 2:
        return {"query": q, "mode": mode, "results": [], "skipped": {}}
    per = max(1, min(limit, 50))
    for kind in wanted:
        try:
            out.extend(table[kind](q, scope, per))
        except _Skip as why:
            skipped[kind] = str(why)
        except Exception as exc:  # noqa: BLE001 - one provider never sinks the rest
            log.warning("search provider %s failed: %s", kind, exc)
            skipped[kind] = f"{type(exc).__name__}: {exc}"
    out.sort(key=lambda h: h.score, reverse=True)
    return {
        "query": q,
        "mode": mode,
        "results": [h.to_dict() for h in out[: per if mode == FULL else min(per, 8)]],
        "skipped": skipped,
    }
