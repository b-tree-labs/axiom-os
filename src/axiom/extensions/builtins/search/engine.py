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
- **library**: Library items by name (title, file name, label, tags) through
  ``library.service.list_items`` and, in full mode, by contents through
  ``library.service.search``; both apply the library's own ``can_read``.
- **chart**: the asker's own saved charts (``charts.saved.listing``), which the
  views store keeps per principal.
- **person**: people by name and role from the Team directory
  (``actors.list``), projected for the asker's role; skipped where that
  directory is not installed.
- **setting**: settings by key name only (never a value).

Every hit says whether it matched by **name** or in **contents**
(``Hit.matched``): the grouped palette renders "By name" and "In contents"
from that field. ``counts`` in the response is computed from the rows
returned, after every bound has applied, so a hidden row is never counted
(MVP-01-A1).

A provider that is not configured here (no chat store, no logbooks, no RAG
store) contributes nothing and says so in ``skipped`` rather than failing the
search — the same contract the MCP primitive keeps.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Any

log = logging.getLogger(__name__)

#: Modes. ``suggest`` is the palette's per-keystroke call: fast providers
#: only, few rows. ``full`` is the results page and the CLI.
SUGGEST = "suggest"
FULL = "full"

KINDS = ("conversation", "logbook", "library", "chart", "person", "setting", "corpus")

#: How a hit matched: its name (title, file name, key) or its contents.
NAME = "name"
CONTENTS = "contents"
MATCHED = (NAME, CONTENTS)


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
    #: ``"name"`` or ``"contents"``: which group the palette shows it under.
    matched: str = CONTENTS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Scope:
    """Who is asking, and over what. ``sites`` None means unbounded (a dev box)."""

    principal: str = ""
    sites: frozenset[str] | None = None
    include_archived: bool = False
    #: False on the per-keystroke palette: providers skip their slow content pass.
    contents: bool = True
    #: One logbook's records only (the Log page's own search box, unit W3); None means every logbook.
    logbook: str | None = None

    def permits(self, site: str) -> bool:
        """A hit with no site is node-wide; one with a site must be in scope."""
        return not site or self.sites is None or site in self.sites


def _named(query: str, *names: str) -> bool:
    q = query.lower()
    return any(q in (n or "").lower() for n in names)


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
                matched=NAME if r.get("matched") == "title" else CONTENTS,
            )
        )
    return hits


def logbooks(query: str, scope: Scope, limit: int) -> list[Hit]:
    from axiom.extensions.builtins.attest import reads, registry, service
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.site_scope import deployment_sites

    sites = scope.sites if scope.sites is not None else deployment_sites()
    if not sites:
        return []
    hits: list[Hit] = []
    for book in registry.all_logbooks():
        if scope.logbook is not None and book.id != scope.logbook:
            continue
        for site in sorted(sites):
            # The database answers the text filter from the record's search projection (W3),
            # newest first; nothing is read and then matched here.
            records = service.records_where(site, book.id, text_=query, limit=limit)
            if records:
                # One access record per log and site, at the door, like every other read (X2).
                reads.record_access(
                    state_dir=get_user_state_dir(), principal=scope.principal or "@search:local",
                    what="search", site=site, logbook=book.id, count=len(records),
                    surface="search", params={"logbook": book.id},
                )  # fmt: skip
            for rec in reversed(records):
                content = rec.get("content") or {}
                text = json.dumps(content, ensure_ascii=False, default=str)
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
                        matched=NAME if _named(query, str(title)) else CONTENTS,
                    )
                )
                if len(hits) >= limit:
                    return hits
    return hits


def corpus(query: str, scope: Scope, limit: int) -> list[Hit]:
    from axiom.extensions.builtins.mcp.platform_primitives import _HANDLERS

    args: dict[str, Any] = {"query": query, "k": limit}
    corpora = variant_corpora()
    if corpora is not None:
        args["corpora"] = corpora
    result = asyncio.run(_HANDLERS["axiom_rag__retrieve"](args))
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
                matched=CONTENTS,
            )
        )
    return hits


def library(query: str, scope: Scope, limit: int) -> list[Hit]:
    """Library items the asker may read, by name and (full mode) by contents.

    Through the library's own service, which applies ``can_read`` (same site,
    and shared there or the asker's own). One caller per site in scope; a site
    the asker's scope does not hold is never asked.
    """
    from axiom.extensions.builtins.library import service
    from axiom.extensions.builtins.library.scope import CallerUnknown, make_caller

    try:
        callers = [
            make_caller(scope.principal, site, surface="search")
            for site in (sorted(scope.sites) if scope.sites is not None else [None])
        ]
    except CallerUnknown as why:
        raise _Skip(str(why)) from why
    hits: list[Hit] = []
    seen: set[str] = set()

    def add(row: dict[str, Any], matched: str, snippet: str = "") -> None:
        if row["id"] in seen or not scope.permits(row.get("site") or ""):
            return
        seen.add(row["id"])
        title = row.get("title") or row.get("file_name") or "Document"
        hits.append(
            Hit(
                kind="library",
                id=str(row["id"]),
                title=str(title),
                snippet=_window(snippet, query) if snippet else str(row.get("file_name") or ""),
                url=f"#/library/{row['id']}",
                score=_score(query, str(title), snippet),
                site=row.get("site") or "",
                meta={"file_name": row.get("file_name"), "category": row.get("category")},
                matched=matched,
            )
        )

    for caller in callers:
        for row in service.list_items(caller, q=query, limit=limit):
            add(row, NAME)
        if scope.contents:
            for row in service.search(caller, query, limit=limit):
                add(row, CONTENTS, row.get("snippet") or "")
    return hits[:limit]


def charts(query: str, scope: Scope, limit: int) -> list[Hit]:
    """The asker's own saved charts; the views store keeps them per principal."""
    if not scope.principal:
        raise _Skip("saved charts belong to a person, and this asker is nobody")
    from axiom.extensions.builtins.charts.saved import listing

    hits: list[Hit] = []
    for row in listing(scope.principal):
        title = row.get("title") or row.get("graph") or "Chart"
        if _named(query, title):
            matched = NAME
        elif _named(query, row.get("group", ""), row.get("kind", "")):
            matched = CONTENTS
        else:
            continue
        hits.append(
            Hit(
                kind="chart",
                id=str(row["id"]),
                title=str(title),
                snippet=" · ".join(x for x in (row.get("group"), row.get("kind")) if x),
                url=f"#/chart/{row['id']}",
                score=_score(query, str(title), row.get("group", "")),
                meta={"group": row.get("group"), "chart_kind": row.get("kind")},
                matched=matched,
            )
        )
        if len(hits) >= limit:
            break
    return hits


def _people() -> list[dict[str, Any]]:
    """People rows from the Team directory (``actors.list``), projected for the asker.

    The directory decides who the asker may see and which fields leave the
    server; search only reads its answer. ``_Skip`` where it is not installed.
    """
    try:
        from axiom.extensions.builtins.actors.skills import list_actors
    except ImportError as why:
        raise _Skip("no people directory on this node (the Team list is not installed)") from why
    result = list_actors({"kind": "person"}, None)
    if not result.ok:
        raise _Skip("; ".join(result.errors) or "the people directory refused")
    return list((result.value or {}).get("actors") or [])


def people(query: str, scope: Scope, limit: int) -> list[Hit]:
    hits: list[Hit] = []
    for row in _people():
        if row.get("kind", "person") != "person" or not scope.permits(row.get("site") or ""):
            continue
        name = str(row.get("name") or row.get("handle") or "")
        roles = [str(r) for r in row.get("roles") or []]
        if _named(query, name, str(row.get("handle") or "")):
            matched = NAME
        elif _named(query, *roles):
            matched = CONTENTS
        else:
            continue
        handle = str(row.get("handle") or name)
        hits.append(
            Hit(
                kind="person",
                id=handle,
                title=name,
                snippet=", ".join(roles),
                url=f"#/team/{handle}",
                score=_score(query, name, " ".join(roles)),
                site=row.get("site") or "",
                meta={"roles": roles},
                matched=matched,
            )
        )
        if len(hits) >= limit:
            break
    return hits


def settings(query: str, scope: Scope, limit: int) -> list[Hit]:
    """Settings by KEY only: a value may carry a credential, so it is never matched or shown."""
    from axiom.extensions.builtins.settings.store import SettingsStore

    hits = [
        Hit(
            kind="setting",
            id=key,
            title=key,
            url=f"#/settings/{key}",
            score=_score(query, key, ""),
            matched=NAME,
        )
        for key in sorted(SettingsStore().all())
        if _named(query, key)
    ]
    return hits[:limit]


def variant_corpora() -> list[str] | None:
    """What a variant's search may retrieve from (VAR-A3); ``None`` on a plain install.

    The same corpora the variant's chat reads (``chat.scope.shared_corpora_for``):
    an isolated variant's tenant plus the community corpus, the in-place one the
    install's shared corpora. Never every corpus, which would include the other
    variants' tenants.
    """
    from axiom.infra.variants import active_variant

    v = active_variant()
    if v is None:
        return None
    from axiom.extensions.builtins.chat.scope import shared_corpora_for

    return list(shared_corpora_for(v.tenant))


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
    "library": library,
    "chart": charts,
    "person": people,
    "setting": settings,
    "corpus": corpus,
}

#: The palette is per keystroke: the corpus (embedding + vector search) is not.
SUGGEST_KINDS = ("conversation", "logbook", "library", "chart", "person")


def counts(results: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Per kind and per ``matched`` group, of the rows actually returned.

    Computed from the final rows and nothing else, so a row a bound removed is
    not counted anywhere (MVP-01-A1): a count is a disclosure too.
    """
    by_kind: dict[str, int] = {}
    by_match = dict.fromkeys(MATCHED, 0)
    for r in results:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        by_match[r.get("matched") or CONTENTS] = by_match.get(r.get("matched") or CONTENTS, 0) + 1
    return {"kind": by_kind, "matched": by_match}


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

    Returns ``{query, mode, results, counts, skipped}``. ``skipped`` maps a provider to
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
    if mode == SUGGEST and scope.contents:
        scope = replace(scope, contents=False)
    for kind in wanted:
        try:
            # A provider's own bound is the first one; this is the backstop:
            # a hit naming a site outside the scope never reaches a row.
            out.extend(h for h in table[kind](q, scope, per) if scope.permits(h.site))
        except _Skip as why:
            skipped[kind] = str(why)
        except Exception as exc:  # noqa: BLE001 - one provider never sinks the rest
            log.warning("search provider %s failed: %s", kind, exc)
            skipped[kind] = f"{type(exc).__name__}: {exc}"
    out.sort(key=lambda h: h.score, reverse=True)
    results = [h.to_dict() for h in out[: per if mode == FULL else min(per, 8)]]
    return {
        "query": q,
        "mode": mode,
        "results": results,
        "counts": counts(results),
        "skipped": skipped,
    }
