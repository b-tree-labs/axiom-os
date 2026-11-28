# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.embedding_model_verify`` and ``data.embedding_model_re_embed`` (P0.2, #2141).

**verify**: check which model made a corpus's vectors, then record it.

Each corpus generation records its embedding model, and writes and queries are held to it. A
generation indexed before records existed has none; ``axi doctor`` lists it. This command
re-embeds a sample of its stored chunks (default 20, one per document) with the configured
embedder and compares each with the stored vector by cosine (the setting
``rag.embedding_verify_cosine``, default 0.999).

**Dry run by default.** ``--apply`` records the model, and only when every sample matches. When
one does not, nothing is written and the report says the corpus needs a re-embed. Recording is a
reversible write: the row can be deleted.

**re-embed**: for the corpus verify refuses. The served generation's chunks are copied into a
candidate generation (text, labels and metadata as they are), embedded there from the stored text
with the configured embedder, and kept in line with the served generation by running it again
(:mod:`axiom.rag.re_embed`). Dry run by default; it never promotes.
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


def re_embed(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Params: ``corpus`` (required), ``exclude_paths`` (a file of patterns, one per line),
    ``batch`` (chunks copied per transaction, default 2000), ``apply``, ``dsn``."""
    corpus = params.get("corpus")
    if not corpus:
        return SkillResult(ok=False, errors=["missing required param: corpus"])
    from axiom.rag import re_embed as core
    from axiom.rag.store import RAGStore

    from .._dsn import resolve_write_dsn

    dsn = resolve_write_dsn(params, env_name=params.get("rag_dsn_env"))  # a writer opens the writer's URL (#2100)
    if not dsn:
        return SkillResult(ok=False, errors=["no RAG DSN (set DP1_RAG_DSN)"])
    if dsn.startswith("sqlite"):
        return SkillResult(
            ok=False,
            errors=[
                "re-embed builds a second generation beside the served one, and the SQLite store serves "
                "one generation per corpus; it needs the Postgres store"
            ],
        )
    patterns: list[str] = []
    if params.get("exclude_paths"):
        try:
            patterns = core.load_exclude_patterns(params["exclude_paths"])
        except OSError as exc:
            return SkillResult(ok=False, errors=[f"cannot read the exclusion file: {exc}"])
    apply = bool(params.get("apply"))
    kwargs = dict(exclude_patterns=patterns, batch=int(params.get("batch") or core.DEFAULT_BATCH))
    store = RAGStore(dsn)
    store.connect()
    try:
        if apply:
            with _authz.action(
                verb="embedding_model_re_embed",
                resource=f"rag://{corpus}",
                classification=Classification.INTERNAL,
                actor=params.get("actor"),
            ):
                report = core.re_embed(store, corpus, apply=True, **kwargs)
        else:
            report = core.re_embed(store, corpus, **kwargs)
    except core.ReEmbedRefused as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    finally:
        store.close()
    if report.get("refused") and "excluded" not in report:
        return SkillResult(ok=False, value=report, errors=[report["refused"]])
    actions = [report["message"]]
    reasons = list(report["excluded"]["by_reason"].items())
    if reasons:
        actions.append(
            "excluded: "
            + "; ".join(f"{n} paths ({why})" for why, n in reasons[:8])
            + (f"; and {len(reasons) - 8} more reasons" if len(reasons) > 8 else "")
        )
    if report.get("refused"):
        actions.append(f"--apply would be refused: {report['refused']}")
    actions += [f"next: {step}" for step in report.get("next_steps", [])]
    return SkillResult(ok=True, value=report, actions_taken=actions)


__all__ = ["re_embed", "verify"]
