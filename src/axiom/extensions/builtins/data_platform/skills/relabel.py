# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.relabel`` — give what a connector already indexed the labels it declares (#1971).

Ingest wrote no access labels before #1971, so every chunk reads as public.
Declaring ``access_tier`` / ``classification`` on a connector covers what it
lands from now on; this brings its existing documents in line.

**Dry run by default.** The report names, per source, how many documents would
tighten, how many would loosen and how many are unchanged, with examples.
``--apply`` writes the tightening changes; a loosening one is written only with
``--allow-loosen`` as well, because it shows a document to people who could not
see it before.

Interim shortcut, like ``data.reindex``: the documents are read from the
connector's bronze manifests (ADR-079 moves labelling into gold curation).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.governance.classification import Classification
from axiom.infra.skills import SkillContext, SkillResult

from .. import _authz
from ..orchestration.relabel import LOOSEN, TIGHTEN, apply_relabel, plan_relabel
from .reindex import _iter_manifests


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Params: ``source`` (a connector name, required), ``apply``, ``allow_loosen``,
    ``bronze_root`` (default: the connector's), ``dsn``."""
    from ..agents.plinth.connectors import declared_access_labels, load_connector

    source = params.get("source")
    if not source:
        return SkillResult(ok=False, errors=["missing required param: source (a connector name)"])
    try:
        config = load_connector(source)
        declared = declared_access_labels(config)
    except (FileNotFoundError, ValueError) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    if declared is None:
        return SkillResult(
            ok=False,
            errors=[
                f"connector {source!r} declares no access labels; set access_tier "
                "(and classification) on it first"
            ],
        )

    from axiom.rag.store import RAGStore, SchemaChangeRefused

    from .._dsn import resolve_write_dsn

    dsn = resolve_write_dsn(params, env_name=config.rag_dsn_env)
    store = RAGStore(dsn)
    try:
        store.connect()
    except SchemaChangeRefused as exc:  # a read-only role: one sentence naming the setting (#2100)
        return SkillResult(ok=False, errors=[str(exc)])
    bronze_root = Path(params.get("bronze_root") or config.bronze_root).expanduser()
    apply = bool(params.get("apply"))
    allow_loosen = bool(params.get("allow_loosen"))
    try:
        plan = plan_relabel(
            _iter_manifests(bronze_root, source),
            store,
            source=source,
            declared=declared,
            default_corpus=config.default_tier or "rag-community",
        )
        report = plan.to_dict()
        written: dict[str, int] | None = None
        if apply:
            with _authz.action(
                verb="relabel",
                resource=f"data-platform://bronze/{source}",
                classification=Classification.INTERNAL,
                actor=params.get("actor"),
            ):
                written = apply_relabel(plan, store, allow_loosen=allow_loosen)
            report["written"] = written
    finally:
        store.close()

    c = report["counts"]
    summary = (
        f"{source}: {c[TIGHTEN]} would tighten, {c[LOOSEN]} would loosen, "
        f"{c['unchanged']} unchanged, {c['not_indexed']} not indexed"
    )
    actions = [summary]
    if written is not None:
        actions.append(
            f"relabelled {written['documents']} documents ({written['chunks']} chunks); "
            f"{written['loosen_withheld']} loosening changes withheld"
            + ("" if allow_loosen else " (pass --allow-loosen to write them)")
        )
    else:
        actions.append("dry run: nothing written (pass --apply to write the tightening changes)")
    return SkillResult(ok=True, value=report, actions_taken=actions)


__all__ = ["run"]
