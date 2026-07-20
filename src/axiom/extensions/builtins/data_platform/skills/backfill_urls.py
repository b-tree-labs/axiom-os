# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.backfill-urls`` — hydrate ``documents.source_url`` / ``source_ref_id``
for one connector (ADR-091).

Corpora indexed before URL capture (e.g. ``data_source=local`` loads that
bypassed the source connector) have no origin id or shareable link. This verb
recovers them: it **re-catalogs the connector's source** (metadata only — no
byte download), builds each item's shareable URL via the provider's
``url_for``, matches the catalog paths to already-indexed documents, and
``UPDATE``s their ``source_url`` / ``source_ref_id``.

``--dry-run`` reports the match rate without writing — always run it first on a
corpus whose path scheme you haven't confirmed. Nothing is re-embedded; only
the two provenance columns are touched.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from axiom.governance.classification import Classification
from axiom.infra.skills import SkillContext, SkillResult

from .. import _authz
from ..agents.plinth.connectors import load_connector
from ..sources import default_source_kind_registry

log = logging.getLogger(__name__)


def _norm_path(p: str) -> str:
    """Normalize a path for cross-scheme matching: strip surrounding space,
    leading slashes, and collapse duplicate slashes."""
    p = (p or "").strip().lstrip("/")
    while "//" in p:
        p = p.replace("//", "/")
    return p


def match_catalog_to_docs(
    catalog: list[tuple[str, str, str | None]],
    doc_paths: list[str],
) -> tuple[dict[str, tuple[str, str | None]], list[str], list[str]]:
    """Match source-catalog entries to already-indexed document paths.

    ``catalog`` is ``(source_path, item_id, url)`` per source item; returns
    ``(matched, unmatched_docs, ambiguous_docs)`` where ``matched`` maps a
    document ``source_path`` → ``(item_id, url)``.

    A local-ingested corpus stores paths with a landing prefix
    (``<root>/<origin path>``) while the source catalogs the bare origin path,
    so this is **boundary-aware suffix matching**: a catalog path matches a doc
    when it equals the doc path, or is a trailing slash-aligned segment of it.
    Bare-filename catalog entries only ever match by equality (never suffix), so
    a root-level ``x.pdf`` can't smear onto every ``*/x.pdf``. A doc that maps to
    more than one distinct catalog entry is reported ambiguous and left
    untouched — never guessed.
    """
    norm_cat = [(_norm_path(sp), iid, url) for (sp, iid, url) in catalog if sp]
    matched: dict[str, tuple[str, str | None]] = {}
    unmatched: list[str] = []
    ambiguous: list[str] = []
    for dp in doc_paths:
        ndp = _norm_path(dp)
        hits = {
            (iid, url)
            for (ncp, iid, url) in norm_cat
            if ncp == ndp or ("/" in ncp and ndp.endswith("/" + ncp))
        }
        if len(hits) == 1:
            matched[dp] = next(iter(hits))
        elif not hits:
            unmatched.append(dp)
        else:
            ambiguous.append(dp)
    return matched, unmatched, ambiguous


def _catalog_source(source) -> list[tuple[str, str]]:
    """Return ``(source_path, item_id)`` for every item, metadata-only.

    Prefers the source's ``catalog()`` (no bytes fetched). Falls back to
    ``list_changed`` + ``fetch`` for kinds without a metadata catalog (pays the
    byte cost — acceptable for a one-shot backfill on small corpora)."""
    catalog = getattr(source, "catalog", None)
    if callable(catalog):
        return [(m.source_path, m.item_id) for m in catalog() if m.source_path]
    out: list[tuple[str, str]] = []
    for item_id in source.list_changed():
        fetched = source.fetch(item_id)
        if fetched.source_path:
            out.append((fetched.source_path, fetched.item_id))
    return out


def _close_source(source) -> None:
    close = getattr(getattr(source, "_api", None), "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    connector = params.get("connector")
    if not connector:
        return SkillResult(ok=False, errors=["missing required param: connector"])
    dry_run = bool(params.get("dry_run", False))

    try:
        config = load_connector(connector, state_dir=ctx.state_dir)
    except FileNotFoundError:
        return SkillResult(ok=False, errors=[f"unknown connector: {connector!r}"])

    provider = default_source_kind_registry().get(config.kind)
    url_for = getattr(provider, "url_for", None)
    if not callable(url_for):
        return SkillResult(
            ok=False,
            errors=[
                f"kind {config.kind!r} exposes no url_for — it is a URL-less "
                "source (exempt per ADR-091); nothing to backfill"
            ],
        )

    corpus = params.get("corpus") or config.default_tier

    dsn = os.environ.get(config.rag_dsn_env) or os.environ.get("DP1_RAG_DSN") or os.environ.get("DATABASE_URL")
    if not dsn:
        return SkillResult(ok=False, errors=[f"RAG DSN unset (env {config.rag_dsn_env!r} / DP1_RAG_DSN / DATABASE_URL)"])
    try:
        import psycopg2
    except ImportError:
        return SkillResult(ok=False, errors=["psycopg2 not installed"])

    # 1. Re-catalog the source (metadata only) and build each item's URL.
    source = provider.construct(config)
    try:
        raw = _catalog_source(source)
    finally:
        _close_source(source)
    catalog = [(sp, iid, url_for(config, iid)) for (sp, iid) in raw]

    # 2. Load the corpus's indexed document paths.
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT source_path FROM documents WHERE corpus = %s", (corpus,))
            doc_paths = [r[0] for r in cur.fetchall()]

        matched, unmatched, ambiguous = match_catalog_to_docs(catalog, doc_paths)

        updated = 0
        if not dry_run and matched:
            with _authz.action(
                verb="backfill-urls",
                resource=f"data-platform://connector/{connector}",
                classification=Classification.INTERNAL,
                actor=params.get("actor"),
            ) as act:
                with conn.cursor() as cur:
                    cur.executemany(
                        "UPDATE documents SET source_url = %s, source_ref_id = %s "
                        "WHERE source_path = %s AND corpus = %s",
                        [(url, iid, dp, corpus) for dp, (iid, url) in matched.items()],
                    )
                    updated = cur.rowcount
                conn.commit()
            audit = f"audit-receipt: {act.receipt_id}"
        else:
            audit = "dry-run: no writes" if dry_run else "no matches to write"
    finally:
        conn.close()

    sample = [
        {"source_path": dp, "source_url": url, "source_ref_id": iid}
        for dp, (iid, url) in list(matched.items())[:10]
    ]
    return SkillResult(
        ok=True,
        value={
            "connector": connector,
            "corpus": corpus,
            "dry_run": dry_run,
            "cataloged": len(catalog),
            "docs_in_corpus": len(doc_paths),
            "matched": len(matched),
            "updated": updated,
            "unmatched": len(unmatched),
            "ambiguous": len(ambiguous),
            "unmatched_sample": unmatched[:10],
            "ambiguous_sample": ambiguous[:10],
            "matched_sample": sample,
        },
        actions_taken=[
            f"cataloged {len(catalog)} source items; {len(doc_paths)} docs in corpus {corpus!r}",
            f"matched {len(matched)} / {len(doc_paths)} (unmatched {len(unmatched)}, ambiguous {len(ambiguous)})",
            audit,
        ],
    )
