# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Re-embed a corpus's stored text into a new generation, beside the one being served (#2141).

For a corpus whose vectors no longer match what the configured embedder makes for the same stored
text (``embedding-model verify`` refuses to record a model for it). Nothing is fetched, extracted
or chunked again: the served generation's chunks are copied into a candidate generation with their
text, access labels and metadata as they are and no vector, and embedded there through
:func:`axiom.rag.ingest.embed_awaiting`, each at its own label. Retrieval reads only the served
generation, so it answers the same until somebody promotes the candidate. This module never does.

**One operation, repeated.** Each run compares the two generations document by document
(:meth:`RAGStore.generation_fingerprints`) and brings the candidate in line: a document the
candidate lacks, or holds differently, is copied again; one the served generation no longer has,
or that is now excluded, is removed; what has no vector is embedded. The first run, a run after an
interruption, and a catch-up after the nightly refresh are that same comparison, which is why an
interrupted run resumes and a run with nothing to do writes nothing.

**``documents`` is not written.** It holds one row per (source_path, corpus), whatever the
generation, and the served generation reads it (names, labels, checksums). The candidate's
documents are the paths its chunks carry.

**The marker.** ``rag_re_embed_run`` holds one row per (corpus, candidate generation) this command
started, written before the candidate is created. A candidate with no row was started by something
else (``axi rag upgrade``) and is refused.

**Exclusions.** A document is left out when its ``source_path`` matches the platform's default
ignore list (:data:`axiom.rag.ingest_router.NON_DOCUMENT_PATTERNS`, the list ingest applies) or a
pattern of the caller's. A pattern has the form of a provenance rule: one ending in ``/`` is a
directory subtree from the start of the path, anything else is a glob over the whole path, where
``*`` also crosses ``/`` (``*/drafts/*``, ``*.bak``).
"""

from __future__ import annotations

import logging
from pathlib import Path

from .ingest_router import Disposition, ProvenanceRule, non_document_reason

log = logging.getLogger(__name__)

RUN_TABLE = "rag_re_embed_run"

_RUN_DDL = f"""\
CREATE TABLE IF NOT EXISTS {RUN_TABLE} (
    corpus               TEXT NOT NULL,
    candidate_generation INTEGER NOT NULL,
    source_generation    INTEGER NOT NULL,
    embedding_model      TEXT NOT NULL,
    started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_run_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at         TIMESTAMPTZ,
    PRIMARY KEY (corpus, candidate_generation)
);
"""

#: Rows copied per statement (each statement is its own transaction).
DEFAULT_BATCH = 2000
#: Texts sent to the embedder per call.
DEFAULT_EMBED_BATCH = 100
#: Compare-and-copy rounds in one run before it stops and says the served generation keeps changing.
MAX_PASSES = 5

_PROBE = "axiom re-embed probe: which model answers"


class ReEmbedRefused(RuntimeError):
    """The re-embed cannot start, or cannot continue, as things stand. Nothing was written by the refusal."""


def load_exclude_patterns(path: str | Path) -> list[str]:
    """The patterns of an exclusion file: one per line; blank lines and ``#`` comments are skipped."""
    lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def exclusion_reason(source_path: str, patterns: list[str]) -> tuple[str, str] | None:
    """Why a document is left out: ``("file", pattern)``, ``("default", reason)``, or ``None`` to keep it."""
    for pattern in patterns:
        if ProvenanceRule(pattern=pattern, disposition=Disposition.EXCLUDE).matches(source_path):
            return "file", pattern
    reason = non_document_reason(source_path)
    return ("default", reason) if reason else None


def _run_row(store, corpus: str, candidate: int) -> dict | None:
    with store._cur() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL AS present", (RUN_TABLE,))
        if not cur.fetchone()["present"]:
            return None
        cur.execute(
            f"SELECT * FROM {RUN_TABLE} WHERE corpus = %s AND candidate_generation = %s",
            (corpus, int(candidate)),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def _candidate_pointer(store, corpus: str) -> int | None:
    with store._cur() as cur:
        cur.execute(
            "SELECT candidate_generation FROM rag_generation_config WHERE corpus = %s", (corpus,)
        )
        row = cur.fetchone()
    return (
        int(row["candidate_generation"])
        if row and row["candidate_generation"] is not None
        else None
    )


def _configured_model() -> str | None:
    """The model the guarded embedder answers with for ordinary text, or ``None`` when none does."""
    from .embeddings import embed_texts_with_model

    vectors, model = embed_texts_with_model([_PROBE], routing_tier="any")
    return model if vectors else None


def _target(store, corpus: str, model: str | None) -> tuple[int, int, bool, str | None]:
    """``(served, candidate, resumed, refusal)``: where a run would write, and why it may not."""
    served = store.served_generation(corpus)
    pointer = _candidate_pointer(store, corpus)
    built = _run_row(store, corpus, served)
    if built and (model is None or built["embedding_model"] == model):
        return (
            served,
            served + 1,
            False,
            (
                f"{corpus} already serves generation {served}, which this command built with "
                f"{built['embedding_model']}; there is nothing to re-embed"
            ),
        )
    candidate = pointer if pointer is not None else served + 1
    run = _run_row(store, corpus, candidate)
    if pointer is not None and run is None:
        return (
            served,
            candidate,
            False,
            (
                f"{corpus} already has candidate generation {candidate}, which this command did not start. "
                f"Promote or discard it first (axi rag discard {candidate} --corpus {corpus})"
            ),
        )
    if run is not None:
        if int(run["source_generation"]) != served:
            return (
                served,
                candidate,
                True,
                (
                    f"generation {candidate} of {corpus} was built from generation {run['source_generation']}, "
                    f"and {corpus} now serves generation {served}; discard it and start again"
                ),
            )
        if model and run["embedding_model"] != model:
            return (
                served,
                candidate,
                True,
                (
                    f"generation {candidate} of {corpus} records {run['embedding_model']}, and the configured "
                    f"embedder is {model}; one generation holds one model's vectors"
                ),
            )
        return served, candidate, True, None
    held = store.generation_chunk_count(corpus, candidate)
    declared = store.declared_embedding_models([corpus], corpus_generation=candidate).get(corpus)
    if held or declared:
        what = f"{held} chunk{'' if held == 1 else 's'}" if held else f"a record of {declared}"
        return (
            served,
            candidate,
            False,
            (
                f"generation {candidate} already holds {what} for {corpus} from a build this command did not "
                "start (a discarded candidate leaves its chunks behind); copying beside them would serve "
                "their vectors. Remove them first"
            ),
        )
    return served, candidate, False, None


class _Diff:
    """What a run would do now: the served generation against the candidate, less the exclusions."""

    def __init__(
        self, store, corpus: str, served: int, candidate: int, patterns: list[str]
    ) -> None:
        self.source = store.generation_fingerprints(corpus, served)
        self.target = store.generation_fingerprints(corpus, candidate)
        self.excluded = {p: why for p in self.source if (why := exclusion_reason(p, patterns))}
        keep = {p: v for p, v in self.source.items() if p not in self.excluded}
        self.kept = keep
        self.to_copy = sorted(p for p, v in keep.items() if self.target.get(p) != v)
        self.to_remove = sorted(p for p in self.target if p not in keep)

    def report(self) -> dict:
        by_reason: dict[str, int] = {}
        for kind, why in self.excluded.values():
            key = why if kind == "default" else f"exclusion file: {why}"
            by_reason[key] = by_reason.get(key, 0) + 1
        return {
            "source": {
                "documents": len(self.source),
                "chunks": sum(n for n, _ in self.source.values()),
            },
            "excluded": {
                "paths": len(self.excluded),
                "chunks": sum(self.source[p][0] for p in self.excluded),
                "by_default": sum(1 for kind, _ in self.excluded.values() if kind == "default"),
                "by_file": sum(1 for kind, _ in self.excluded.values() if kind == "file"),
                "by_reason": dict(sorted(by_reason.items(), key=lambda kv: -kv[1])),
            },
            "to_copy": {
                "documents": len(self.to_copy),
                "chunks": sum(self.kept[p][0] for p in self.to_copy),
            },
            "to_remove": {
                "documents": len(self.to_remove),
                "chunks": sum(self.target[p][0] for p in self.to_remove),
            },
        }


def _batches(paths: list[str], sizes: dict[str, tuple[int, str]], limit: int):
    """Whole documents, grouped until a group holds about ``limit`` chunks."""
    group: list[str] = []
    rows = 0
    for path in paths:
        group.append(path)
        rows += sizes[path][0]
        if rows >= limit:
            yield group
            group, rows = [], 0
    if group:
        yield group


def next_steps(corpus: str, served: int, candidate: int) -> list[str]:
    return [
        f"axi data embedding-model verify --corpus {corpus} --generation {candidate}",
        f"axi rag promote {candidate} --corpus {corpus}",
        f"axi rag rollback {served} --corpus {corpus}   (after a promote, to serve generation {served} again)",
    ]


def re_embed(
    store,
    corpus: str,
    *,
    exclude_patterns: list[str] | tuple[str, ...] = (),
    batch: int = DEFAULT_BATCH,
    embed_batch: int = DEFAULT_EMBED_BATCH,
    apply: bool = False,
) -> dict:
    """Bring a candidate generation of ``corpus`` in line with the served one, re-embedded.

    A dry run (the default) reports what would be copied, removed, excluded and embedded, and the
    model that would be recorded, and writes nothing; a state that refuses the run is reported
    under ``refused``. With ``apply`` a refusal raises :class:`ReEmbedRefused` before anything is
    written; otherwise the candidate is created with its model recorded first, documents are
    copied ``batch`` chunks at a time, the vectorless chunks are embedded, and the comparison is
    repeated until the two generations agree. The candidate is never promoted here.
    """
    if not callable(getattr(store, "copy_documents_to_generation", None)):
        raise ReEmbedRefused(
            "this store serves one generation per corpus, so there is no second one to build beside it; "
            "re-embed needs the Postgres store"
        )
    store.connect()
    if not (
        getattr(store, "_has_generation_config", False)
        and getattr(store, "_has_embedding_declarations", False)
    ):
        raise ReEmbedRefused(
            "this database has no generation tables (rag_generation_config, rag_corpus_embedding_model); "
            "connect once with schema ensure first"
        )
    patterns = [p for p in exclude_patterns if p]
    batch = max(1, int(batch))
    model = _configured_model()
    served, candidate, resumed, refusal = _target(store, corpus, model)
    report: dict = {
        "corpus": corpus,
        "apply": bool(apply),
        "served_generation": served,
        "candidate_generation": candidate,
        "model": model,
        "resumed": resumed,
        "promoted": False,
    }
    no_embedder = (
        None
        if model
        else "no embedder answers here for ordinary text, so there is no model to record and nothing to "
        "embed with; configure one (axi doctor) and run again"
    )
    if apply and (refusal or no_embedder):
        raise ReEmbedRefused(refusal or no_embedder)
    if refusal:
        report.update(refused=refusal, message=f"refused: {refusal}")
        return report

    if not apply:
        diff = _Diff(store, corpus, served, candidate, patterns)
        report.update(diff.report())
        report["distinct_texts"] = store.distinct_texts_to_embed(
            corpus,
            from_generation=served,
            paths=diff.to_copy,
            to_generation=candidate,
            replaced=diff.to_copy + diff.to_remove,
        )
        if no_embedder:
            report["refused"] = no_embedder
        report["message"] = (
            f"dry run: would copy {report['to_copy']['chunks']} chunks of {report['to_copy']['documents']} "
            f"documents from generation {served} into generation {candidate}, leave out "
            f"{report['excluded']['paths']} excluded paths ({report['excluded']['chunks']} chunks), embed "
            f"{report['distinct_texts']} distinct texts and record {model or 'no model (no embedder answers)'}; "
            "nothing written (pass --apply)"
        )
        return report

    _start(store, corpus, served, candidate, model)
    from .ingest import embed_awaiting

    totals = {
        "copied_d": 0,
        "copied_c": 0,
        "removed_d": 0,
        "removed_c": 0,
        "distinct": 0,
        "embedded": 0,
    }
    awaiting = 0
    first: dict | None = None
    converged = False
    diff = None
    for attempt in range(MAX_PASSES):
        diff = _Diff(store, corpus, served, candidate, patterns)
        if first is None:
            first = diff.report()
        if attempt and not diff.to_copy and not diff.to_remove:
            converged = True
            break
        for group in _batches(diff.to_remove, diff.target, batch):
            totals["removed_c"] += store.remove_documents_from_generation(
                corpus, group, corpus_generation=candidate
            )
            totals["removed_d"] += len(group)
        for group in _batches(diff.to_copy, diff.kept, batch):
            totals["copied_c"] += store.copy_documents_to_generation(
                corpus, group, from_generation=served, to_generation=candidate
            )
            totals["copied_d"] += len(group)
        out = embed_awaiting(
            store,
            corpus,
            batch=embed_batch,
            corpus_generation=candidate,
            reuse_identical=True,
            preserve_updated_at=True,
        )
        totals["distinct"] += out["distinct_embedded"]
        totals["embedded"] += out["embedded"]
        awaiting = out["waiting"]
    held = diff.target if converged else store.generation_fingerprints(corpus, candidate)
    if converged:
        with store._cur() as cur:
            cur.execute(
                f"UPDATE {RUN_TABLE} SET completed_at = now(), last_run_at = now() "
                "WHERE corpus = %s AND candidate_generation = %s",
                (corpus, candidate),
            )
    report.update(
        source=first["source"],
        excluded=first["excluded"],
        copied={"documents": totals["copied_d"], "chunks": totals["copied_c"]},
        removed={"documents": totals["removed_d"], "chunks": totals["removed_c"]},
        embedded_distinct=totals["distinct"],
        reused=totals["embedded"] - totals["distinct"],
        awaiting=awaiting,
        candidate={"documents": len(held), "chunks": sum(n for n, _ in held.values())},
        converged=converged,
        next_steps=next_steps(corpus, served, candidate),
    )
    report["message"] = (
        f"generation {candidate} of {corpus}: copied {totals['copied_c']} chunks of {totals['copied_d']} "
        f"documents, removed {totals['removed_c']}, left out {first['excluded']['paths']} excluded paths "
        f"({first['excluded']['chunks']} chunks); embedded {totals['distinct']} distinct texts with {model}, "
        f"reused {report['reused']} vectors; {awaiting} chunks still await an admitted embedder. "
        f"Generation {served} is still the one served"
        + (
            ""
            if converged
            else f". The served generation changed during each of {MAX_PASSES} rounds; run again to catch up"
        )
    )
    return report


def _start(store, corpus: str, served: int, candidate: int, model: str) -> None:
    """Write the marker, point the corpus at its candidate and record the model: in that order.

    The marker first, so a run that stops between any two of these is recognised as this
    command's own on the next run. Every step is a no-op when it has already happened.
    """
    from .generation import GenerationManager

    with store._cur() as cur:
        cur.execute(_RUN_DDL)
        cur.execute(
            f"INSERT INTO {RUN_TABLE} (corpus, candidate_generation, source_generation, embedding_model) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (corpus, candidate_generation) DO UPDATE SET "
            "last_run_at = now(), completed_at = NULL",
            (corpus, candidate, served, model),
        )
    if _candidate_pointer(store, corpus) != candidate:
        manager = GenerationManager(store)
        manager.get_active_generation(corpus)  # makes the corpus's row when it has none
        made = manager.create_candidate(corpus)
        if made != candidate:
            raise ReEmbedRefused(
                f"expected to create candidate generation {candidate} for {corpus}, and got {made}"
            )
    recorded = store.record_embedding_model(corpus, candidate, model)
    if recorded != model:
        raise ReEmbedRefused(
            f"generation {candidate} of {corpus} records {recorded}, and the configured embedder is {model}"
        )


__all__ = [
    "DEFAULT_BATCH",
    "RUN_TABLE",
    "ReEmbedRefused",
    "exclusion_reason",
    "load_exclude_patterns",
    "next_steps",
    "re_embed",
]
