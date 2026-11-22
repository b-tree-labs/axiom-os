# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.embedding_model_verify``: check which model made a corpus's vectors, then record it (P0.2).

Each corpus generation records its embedding model, and writes and queries are held to it. A
generation indexed before records existed has none; ``axi doctor`` lists it. This command
re-embeds a sample of its stored chunks (default 20, one per document) with the configured
embedder and compares each with the stored vector by cosine (the setting
``rag.embedding_verify_cosine``, default 0.999).

**Dry run by default.** ``--apply`` records the model, and only when every sample matches. When
one does not, nothing is written and the report says the corpus needs a re-embed. Recording is a
reversible write: the row can be deleted.
"""

from __future__ import annotations

from typing import Any

from axiom.governance.classification import Classification
from axiom.infra.skills import SkillContext, SkillResult

from .. import _authz


def verify(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Params: ``corpus`` (required), ``generation``, ``sample`` (default 20), ``threshold``,
    ``apply``, ``dsn``."""
    corpus = params.get("corpus")
    if not corpus:
        return SkillResult(ok=False, errors=["missing required param: corpus"])
    from axiom.rag.embeddings import verify_embedding_model
    from axiom.rag.store import RAGStore, SchemaChangeRefused

    from .._dsn import resolve_write_dsn

    dsn = resolve_write_dsn(params, env_name=params.get("rag_dsn_env"))
    if not dsn:
        return SkillResult(ok=False, errors=["no RAG DSN (set DP1_RAG_DSN)"])
    store = RAGStore(dsn)
    try:
        store.connect()
    except SchemaChangeRefused as exc:  # a read-only role: one sentence naming the setting (#2100)
        return SkillResult(ok=False, errors=[str(exc)])
    apply = bool(params.get("apply"))
    kwargs = dict(
        corpus_generation=params.get("generation"),
        sample=int(params.get("sample") or 20),
        threshold=params.get("threshold"),
    )
    try:
        if apply:
            with _authz.action(
                verb="embedding_model_verify",
                resource=f"rag://{corpus}",
                classification=Classification.INTERNAL,
                actor=params.get("actor"),
            ):
                report = verify_embedding_model(store, corpus, apply=True, **kwargs)
        else:
            report = verify_embedding_model(store, corpus, **kwargs)
    finally:
        store.close()
    ok = bool(report["matched"]) or report["sampled"] == 0
    return SkillResult(
        ok=ok,
        value=report,
        actions_taken=[report["message"]],
        errors=[] if ok else [report["message"]],
    )


__all__ = ["verify"]
