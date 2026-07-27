# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI entry point for the RAG subsystem.

Usage::

    python -m axiom.rag index [path]        # index workspace docs
    python -m axiom.rag search "query"      # hybrid search
    python -m axiom.rag status              # per-corpus statistics
    python -m axiom.rag load-community      # load community corpus dump
    python -m axiom.rag sync org            # sync org corpus
    python -m axiom.rag reindex             # force re-index all docs

Legacy aliases: ``ingest`` → ``index``, ``stats`` → ``status``
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def _load_env() -> None:
    """Try to load DATABASE_URL from .env or axi settings if not already set."""
    if os.environ.get("DATABASE_URL"):
        return
    # Try .env file
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[4] / ".env"):
        if candidate.is_file():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    key, val = key.strip(), val.strip().strip("\"'")
                    if key and key not in os.environ:
                        os.environ[key] = val
            break
    # Fall back to axi settings
    if not os.environ.get("DATABASE_URL"):
        try:
            from axiom.extensions.builtins.settings.store import SettingsStore

            url = SettingsStore().get("rag.database_url", "")
            if url:
                os.environ["DATABASE_URL"] = url
        except Exception:
            pass

    # Fall back to auto-provisioned PG with stored password
    if not os.environ.get("DATABASE_URL"):
        try:
            from axiom.setup.secrets import get_secret

            pg_pass = get_secret("AXIOM_PG_PASSWORD")
            if pg_pass:
                os.environ["DATABASE_URL"] = f"postgresql://axiom:{pg_pass}@localhost:5432/axiom_db"
        except Exception:
            pass


def _get_store():
    from .store import RAGStore

    url = os.environ.get("DATABASE_URL")
    if not url:
        print(
            "ERROR: No RAG database configured.\n"
            '  Set via: axi settings set rag.database_url "postgresql://..."\n'
            "  Or:      export DATABASE_URL=postgresql://...",
            file=sys.stderr,
        )
        sys.exit(1)
    store = RAGStore(url)
    store.connect()
    return store


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def cmd_index(args: argparse.Namespace) -> None:
    """Index one or more paths (or the default repo paths)."""
    from axiom import REPO_ROOT

    from .ingest import ingest_file, ingest_repo
    from .store import CORPUS_INTERNAL

    corpus = getattr(args, "corpus", CORPUS_INTERNAL)
    store = _get_store()
    try:
        if args.paths:
            from .ingest import IngestStats, ingest_file, ingest_path

            total = IngestStats()
            for raw in args.paths:
                p = Path(raw).resolve()
                if p.is_dir():
                    total += ingest_path(p, store, corpus=corpus)
                elif p.is_file():
                    total += ingest_file(p, store, repo_root=p.parent, corpus=corpus)
                else:
                    print(f"WARNING: path not found: {p}", file=sys.stderr)
        else:
            total = ingest_repo(REPO_ROOT, store, corpus=corpus)

        print(
            f"Indexed: {total.files_indexed} files, "
            f"{total.chunks_created} chunks  "
            f"({total.files_unchanged} unchanged)  "
            f"[corpus: {corpus}]"
        )
        report = total.drop_report()
        if report:
            print(f"Dropped (not indexed): {report}", file=sys.stderr)
    finally:
        store.close()


def cmd_search(args: argparse.Namespace) -> None:
    from .embeddings import embed_texts

    store = _get_store()
    try:
        try:
            embs = embed_texts([args.query])
        except Exception as exc:
            log.warning("Embedding skipped (using text-only search): %s", exc)
            embs = None
        results = store.search(
            query_embedding=embs[0] if embs else None,
            query_text=args.query,
            limit=args.limit,
        )
        if not results:
            print("No results.")
            return

        for i, r in enumerate(results, 1):
            print(f"\n--- Result {i} (score: {r.combined_score:.4f}, corpus: {r.corpus}) ---")
            print(f"  Source: {r.source_path} (chunk {r.chunk_index})")
            print(f"  Title:  {r.source_title}")
            print(f"  {r.chunk_text[:300]}...")
    finally:
        store.close()


def _build_model_fn(*, system: str = ""):
    """Default model_fn: wraps :class:`axiom.infra.gateway.Gateway`.

    Returns a callable matching the eval-harness ModelFn signature
    ``(prompt, *, context=None) -> str``. The context (if present) is
    prepended to the prompt as supplied retrieval context.

    Patched in tests so the eval-harness wiring is exercised without
    requiring a live LLM.
    """
    from axiom.infra.gateway import Gateway
    gw = Gateway()

    def call(prompt: str, *, context: str | None = None) -> str:
        full = prompt if not context else (
            f"Context:\n{context}\n\nQuestion: {prompt}\n\nAnswer:"
        )
        try:
            resp = gw.complete(prompt=full, system=system, task="extraction")
            # GatewayResponse.text per src/axiom/infra/gateway.py:148.
            return getattr(resp, "text", None) or getattr(resp, "content", None) or ""
        except Exception as exc:  # noqa: BLE001
            log.warning("gateway.complete failed: %s", exc)
            return ""

    return call


def cmd_eval(args: argparse.Namespace) -> None:
    """Run the RAG eval harness against a YAML question set.

    Default mode runs baseline + with-retrieval and prints the lift.
    ``--no-retrieval`` runs baseline only (no RAG).

    Closes the 'Qwen with/without RAG' deliverable from the lakehouse
    epic (#386). Model + retriever are pluggable; default model wraps
    the gateway, default retriever wraps RAGStore.search.
    """
    from pathlib import Path

    from .eval import (
        Citation,
        compare_with_and_without_retrieval,
        load_questions,
        run_eval,
    )

    qpath = Path(args.questions)
    if not qpath.exists() or not qpath.is_file():
        print(f"questions file not found: {qpath}", file=sys.stderr)
        sys.exit(1)

    questions = load_questions(qpath)
    if args.limit:
        questions = questions[: args.limit]

    model_fn = _build_model_fn()
    store = _get_store()
    try:
        def retriever_fn(query: str) -> list[Citation]:
            hits = store.search(query_text=query, limit=args.retrieval_k)
            return [
                Citation(
                    source_path=h.source_path,
                    chunk_text=getattr(h, "chunk_text", ""),
                    chunk_index=getattr(h, "chunk_index", 0),
                    score=getattr(h, "combined_score", 0.0),
                )
                for h in hits
            ]

        if args.no_retrieval:
            report = run_eval(questions, model_fn=model_fn, retriever_fn=None)
            _print_report("baseline (no retrieval)", report)
        else:
            diff = compare_with_and_without_retrieval(
                questions, model_fn=model_fn, retriever_fn=retriever_fn,
            )
            _print_report("baseline (no retrieval)", diff.baseline)
            _print_report("with retrieval", diff.with_retrieval)
            print(f"\nlift (with - baseline) on mean answer score: {diff.lift:+.3f}")
    finally:
        store.close()


def _print_report(label: str, report) -> None:
    print(f"\n=== {label} ===")
    print(f"  total: {report.total}")
    print(f"  passed: {report.passed}")
    print(f"  mean answer score: {report.mean_answer_score:.3f}")
    print(f"  mean citation score: {report.mean_citation_score:.3f}")
    print(f"  mean latency (ms): {report.mean_latency_ms}")


def cmd_status(args: argparse.Namespace) -> None:
    """Show per-corpus statistics."""
    store = _get_store()
    try:
        s = store.stats()
        print("RAG Index Status")
        print(f"  Total documents: {s['total_documents']}")
        print(f"  Total chunks:    {s['total_chunks']}")
        print()
        print(f"  {'Corpus':<20} {'Docs':>6} {'Chunks':>8}")
        print(f"  {'-' * 20} {'-' * 6} {'-' * 8}")
        for corpus in ("rag-community", "rag-org", "rag-internal"):
            docs = s["documents_by_corpus"].get(corpus, 0)
            chunks = s["chunks_by_corpus"].get(corpus, 0)
            label = corpus.replace("rag-", "")
            print(f"  {label:<20} {docs:>6} {chunks:>8}")
    finally:
        store.close()


def cmd_load_community(args: argparse.Namespace) -> None:
    """Load the community corpus from a pre-built dump file."""
    from pathlib import Path

    store = _get_store()
    try:
        # If no path given, look for bundled dump
        if args.dump_path:
            dump = Path(args.dump_path)
        else:
            # Look for bundled community dump
            try:
                pkg_dir = Path(__file__).resolve().parents[1] / "data" / "rag"
                candidates = sorted(pkg_dir.glob("community-v*.sql")) + sorted(
                    pkg_dir.glob("community-v*.pgdump")
                )
                if not candidates:
                    print(
                        "No community corpus dump found.\n"
                        "  Expected: src/axiom/data/rag/community-v*.sql\n"
                        "  Download: axi update --community-rag",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                dump = candidates[-1]  # latest by name sort
            except Exception as exc:
                print(f"ERROR locating community dump: {exc}", file=sys.stderr)
                sys.exit(1)

        print(f"Loading community corpus from {dump} ...")
        store.load_community_dump(dump)
        s = store.stats()
        chunks = s["chunks_by_corpus"].get("rag-community", 0)
        docs = s["documents_by_corpus"].get("rag-community", 0)
        print(f"Community corpus loaded: {docs} documents, {chunks} chunks")
    finally:
        store.close()


def cmd_sync(args: argparse.Namespace) -> None:
    """Sync org corpus (v1: manual rsync instructions)."""
    target = getattr(args, "target", "org")
    if target == "org":
        # Configure your org corpus source in runtime/config/rag.toml
        # Example: org_corpus_source = "server.example.com:/shared/org-corpus/"
        print(
            "To sync the org corpus, run:\n"
            "\n"
            "  rsync -avz --progress \\\n"
            "    <your-server>:/path/to/org-corpus/ \\\n"
            "    runtime/knowledge/org-corpus/\n"
            "\n"
            "  axi rag index runtime/knowledge/org-corpus/ --corpus rag-org\n"
            "\n"
            "Configure your server path in runtime/config/rag.toml.\n"
            "Automated sync (axi rag sync org --auto) will be available in v2."
        )
    else:
        print(f"Unknown sync target: {target}", file=sys.stderr)
        sys.exit(1)


def cmd_watch(args: argparse.Namespace) -> None:
    """Watch workspace directories and re-index changed files automatically."""
    from axiom import REPO_ROOT

    from .store import CORPUS_INTERNAL
    from .watcher import watch

    corpus = getattr(args, "corpus", CORPUS_INTERNAL)
    store = _get_store()
    try:
        watch(REPO_ROOT, store, corpus=corpus, quiet=args.quiet)
    finally:
        store.close()


def cmd_reindex(args: argparse.Namespace) -> None:
    """Force re-index by clearing checksums and re-running index."""
    from axiom import REPO_ROOT

    from .ingest import ingest_repo
    from .store import CORPUS_INTERNAL

    corpus = getattr(args, "corpus", CORPUS_INTERNAL)
    store = _get_store()
    try:
        # Clear existing corpus so checksums don't skip anything
        deleted = store.delete_corpus(corpus)
        print(f"Cleared {deleted} chunks from {corpus}")
        stats = ingest_repo(REPO_ROOT, store, corpus=corpus)
        print(
            f"Re-indexed: {stats.files_indexed} files, "
            f"{stats.chunks_created} chunks  [corpus: {corpus}]"
        )
    finally:
        store.close()


# Legacy alias
def cmd_ingest_advanced(args: argparse.Namespace) -> None:
    """Durable, resumable ingest of a large corpus (spec-rag-ingest-advanced).

    Preflight always runs (fast-fail). `--dry-run` stops there. The live path
    uses the hardened ingest engine (file-level checksum resume + honest drop
    reporting); batch-level checkpoint/resume and federated `--target` are
    wired in follow-up units.
    """
    import os
    import shutil
    from pathlib import Path

    from .ingest_cli import cmd_dry_run, format_preflight
    from .ingest_preflight import run_preflight

    paths = [Path(p) for p in (args.paths or [])]
    if not paths:
        print("ERROR: provide at least one path to ingest", file=sys.stderr)
        sys.exit(2)

    target = getattr(args, "target", "local")
    if target != "local":
        print(
            f"ERROR: federated target {target!r} is not supported yet "
            "(forthcoming); use --target local",
            file=sys.stderr,
        )
        sys.exit(2)

    corpus = args.corpus
    ckpt_dir = Path(getattr(args, "checkpoint_dir", ".axi/rag-ingest"))

    rules = None
    rules_path = getattr(args, "rules", None) or os.environ.get("AXIOM_RAG_RULES")
    if rules_path:
        from .ingest_router import load_rules_file

        try:
            rules = load_rules_file(rules_path)
        except Exception as exc:
            print(f"ERROR: could not load rules {rules_path}: {exc}", file=sys.stderr)
            sys.exit(2)

    def reachable_fn():
        if not os.environ.get("DATABASE_URL"):
            return (False, "DATABASE_URL not set")
        return (True, "ok")

    def free_bytes_fn():
        probe = ckpt_dir if ckpt_dir.exists() else Path.cwd()
        return shutil.disk_usage(probe).free

    if getattr(args, "dry_run", False):
        rc = cmd_dry_run(
            paths, corpus=corpus, target=target,
            reachable_fn=reachable_fn, free_bytes_fn=free_bytes_fn,
        )
        sys.exit(rc)

    # Footgun guard: a live ingest into a shared tier with no provenance rules
    # could bulk-ingest controlled/proprietary content ungated. Require rules
    # (or an explicit --no-rules acknowledgement) for shared tiers.
    if rules is None and corpus in {"rag-org", "rag-community"} and not getattr(
        args, "no_rules", False
    ):
        print(
            f"ERROR: refusing to ingest to shared tier '{corpus}' without provenance rules — "
            "an ungated bulk ingest could expose controlled or proprietary content. "
            "Pass --rules <file.toml> (recommended), set AXIOM_RAG_RULES, or --no-rules to override.",
            file=sys.stderr,
        )
        sys.exit(2)

    report = run_preflight(paths, reachable_fn=reachable_fn, free_bytes_fn=free_bytes_fn)
    if report.abort_reason:
        print(format_preflight(report, corpus=corpus, target=target), file=sys.stderr)
        sys.exit(1)

    from .ingest import IngestStats, ingest_path

    store = _get_store()
    try:
        total = IngestStats()
        for p in paths:
            total += ingest_path(p.resolve(), store, corpus=corpus, rules=rules)
    finally:
        store.close()

    print(
        f"Indexed: {total.files_indexed} files, {total.chunks_created} chunks  "
        f"({total.files_unchanged} unchanged)  [corpus: {corpus}]"
    )
    drops = total.drop_report()
    if drops:
        print(f"Dropped (not indexed): {drops}", file=sys.stderr)


def _ingest_one_file(
    path: Path,
    store,
    *,
    source_path: str | None = None,
    corpus: str = "rag-internal",
    chunking_tier: str = "fixed",
) -> tuple[int, int]:
    """Index a single file. Thin wrapper around ``ingest.ingest_file`` so
    tests can stub the heavyweight ingest path. Returns
    ``(files_indexed, chunks_created)``.
    """
    from .ingest import ingest_file

    repo_root = path.parent
    stats = ingest_file(
        path,
        store,
        repo_root=repo_root,
        corpus=corpus,
        chunking_tier=chunking_tier,
    )
    # If source_path was overridden and the file actually got indexed,
    # rewrite the recorded path. Cheap one-row UPDATE.
    if source_path and stats.files_indexed:
        local_rel = str(path.relative_to(repo_root))
        try:
            store._cur().execute(
                "UPDATE documents SET source_path = %s WHERE source_path = %s AND corpus = %s",
                (source_path, local_rel, corpus),
            )
            store._cur().execute(
                "UPDATE chunks SET source_path = %s WHERE source_path = %s AND corpus = %s",
                (source_path, local_rel, corpus),
            )
        except Exception:
            pass
    return (stats.files_indexed, stats.chunks_created)


def cmd_add(args: argparse.Namespace) -> None:
    """Add a single file to a RAG corpus (operator one-shot).

    Wraps the existing ingest path with a tight single-file CLI surface
    for manual operator use. Dedup-by-checksum (same path, same MD5 =
    no-op) is honored by the underlying ``ingest_file``; the
    ``documents.content_hash`` column powers future silver-tier
    cross-path dedup (lakehouse epic Day 4).

    ``--source-path`` records a canonical origin (Box folder URI,
    GitHub blob URL, etc.) instead of the local filesystem path.
    """
    path = Path(args.path).resolve()
    if not path.exists() or not path.is_file():
        print(f"not found: {args.path!r} does not exist or is not a file",
              file=sys.stderr)
        sys.exit(1)

    store = _get_store()
    try:
        indexed, chunks = _ingest_one_file(
            path,
            store,
            source_path=getattr(args, "source_path", None),
            corpus=args.corpus,
            chunking_tier=getattr(args, "chunking_tier", "fixed"),
        )
        if indexed:
            print(f"Indexed {path.name}: {chunks} chunks  [corpus: {args.corpus}]")
        else:
            print(f"Unchanged (already indexed with same checksum): {path.name}")
    finally:
        store.close()


def cmd_remove(args: argparse.Namespace) -> None:
    """Remove a document (and all its chunks) from a corpus by name or path.

    Resolution:
    - ``name`` containing ``/`` → exact ``source_path`` match.
    - Bare filename → basename match across the corpus.

    Outcomes:
    - 0 matches → exit 1 with a "not found" message (idempotent-fail).
    - 1 match  → preview line; ``--yes`` confirms deletion.
    - N matches → list all + exit 2 unless ``--all`` is set.

    Closes the "I just want this one file gone" gap that
    ``axi rag audit --purge`` was too heavy for. Filed alongside
    Shayan's 2026-06-01 request.
    """
    store = _get_store()
    try:
        matches = store.find_documents_by_name(args.name, args.corpus)
        if not matches:
            print(
                f"not found: {args.name!r} in corpus {args.corpus!r}",
                file=sys.stderr,
            )
            sys.exit(1)

        if len(matches) > 1 and not getattr(args, "all", False):
            print(
                f"ambiguous: {args.name!r} matches {len(matches)} documents in "
                f"corpus {args.corpus!r}:",
                file=sys.stderr,
            )
            for m in matches:
                print(f"  {m['source_path']}  ({m['chunk_count']} chunks)",
                      file=sys.stderr)
            print(
                "\nRe-run with the full --name <source_path> for one of them, "
                "or pass --all to remove every match.",
                file=sys.stderr,
            )
            sys.exit(2)

        total_chunks = sum(m["chunk_count"] for m in matches)

        if not getattr(args, "yes", False):
            for m in matches:
                print(f"Would remove: {m['source_path']}  ({m['chunk_count']} chunks)")
            print(f"\n{len(matches)} document(s), {total_chunks} chunk(s) total. "
                  "Re-run with --yes to delete.")
            return

        for m in matches:
            store.delete_document(m["source_path"], args.corpus)
        print(f"Removed {len(matches)} document(s) ({total_chunks} chunks) "
              f"from corpus {args.corpus!r}.")
    finally:
        store.close()


def cmd_audit(args: argparse.Namespace) -> None:
    """Audit an existing corpus's source paths against provenance rules.

    Read-only by default; ``--purge`` deletes EXCLUDE-flagged documents
    (``--yes`` to skip confirmation). Finds controlled/proprietary content that
    was ingested before the rules existed — e.g. before a live store was gated.
    """
    import os

    from .ingest_router import Disposition, audit_paths, load_rules_file

    corpus = args.corpus
    rules_path = getattr(args, "rules", None) or os.environ.get("AXIOM_RAG_RULES")
    if not rules_path:
        print("ERROR: audit requires --rules <file.toml> (or AXIOM_RAG_RULES)", file=sys.stderr)
        sys.exit(2)
    try:
        rules = load_rules_file(rules_path)
    except Exception as exc:
        print(f"ERROR: could not load rules {rules_path}: {exc}", file=sys.stderr)
        sys.exit(2)

    store = _get_store()
    try:
        paths = store.list_document_paths(corpus)
        report = audit_paths(paths, rules)
        print(
            f"Audited {report.total} documents in {corpus}: "
            f"{report.excluded} excluded, {report.quarantined} quarantined"
        )
        for p, disp, reason in report.flagged:
            print(f"  {disp.value.upper():10} {p}  ({reason})")

        if getattr(args, "purge", False):
            to_purge = [p for p, d, _ in report.flagged if d is Disposition.EXCLUDE]
            if not to_purge:
                print("Nothing to purge.")
            elif not getattr(args, "yes", False):
                print(
                    f"\nWould purge {len(to_purge)} EXCLUDE-flagged document(s). "
                    "Re-run with --yes to delete.",
                    file=sys.stderr,
                )
            else:
                for p in to_purge:
                    store.delete_document(p, corpus)
                print(f"Purged {len(to_purge)} excluded document(s) from {corpus}.")
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Generation management commands
# ---------------------------------------------------------------------------


def cmd_upgrade(args: argparse.Namespace) -> None:
    """Build a new RAG generation with semantic chunking."""
    from pathlib import Path

    from .upgrade import build_generation

    store = _get_store()
    try:
        stats = build_generation(
            store=store,
            source_path=Path(args.source),
            corpus=args.corpus,
            chunking_tier=args.tier,
            auto_backup=not args.no_backup,
        )
        if stats.success:
            print(
                f"Generation {stats.generation} built: {stats.files_processed} files, "
                f"{stats.chunks_created} chunks (tier={stats.chunking_tier})"
            )
            if stats.backup_path:
                print(f"  Backup: {stats.backup_path}")
        else:
            print(f"Build failed: {stats.error}", file=sys.stderr)
            sys.exit(1)
    finally:
        store.close()


def cmd_promote(args: argparse.Namespace) -> None:
    store = _get_store()
    try:
        from .generation import GenerationManager

        gm = GenerationManager(store)
        gm.promote(args.corpus, args.generation)
        print(f"Promoted generation {args.generation} for {args.corpus}")
    finally:
        store.close()


def cmd_rollback(args: argparse.Namespace) -> None:
    store = _get_store()
    try:
        from .generation import GenerationManager

        gm = GenerationManager(store)
        gm.rollback(args.corpus, args.generation)
        print(f"Rolled back {args.corpus} to generation {args.generation}")
    finally:
        store.close()


def cmd_discard(args: argparse.Namespace) -> None:
    store = _get_store()
    try:
        from .generation import GenerationManager

        gm = GenerationManager(store)
        gm.discard(args.corpus, args.generation)
        print(f"Discarded generation {args.generation} for {args.corpus}")
    finally:
        store.close()


def cmd_benchmark(args: argparse.Namespace) -> None:
    """Run A/B quality benchmark."""
    store = _get_store()
    try:
        from .benchmark import run_ab_benchmark

        # Use a minimal set of gold queries for quick evaluation
        gold = [
            {"query": "emergency core cooling systems", "keywords": ["ECCS", "cooling", "core"]},
            {"query": "MSRE fuel salt composition", "keywords": ["LiF", "BeF2", "fuel", "salt"]},
            {"query": "TRIGA fuel element", "keywords": ["zirconium", "hydride", "TRIGA"]},
            {"query": "radiation protection dose limits", "keywords": ["ALARA", "dose", "rem"]},
            {"query": "criticality safety validation", "keywords": ["keff", "subcritical"]},
            {"query": "neutron transport methods", "keywords": ["neutron", "transport"]},
            {"query": "reactor safety analysis", "keywords": ["safety", "analysis", "reactor"]},
            {"query": "molten salt reactor", "keywords": ["molten", "salt", "MSR"]},
            {"query": "graphite moderator", "keywords": ["graphite", "moderator"]},
            {"query": "NRC regulatory requirements", "keywords": ["NRC", "regulation"]},
        ]
        tiers = args.compare
        report = run_ab_benchmark(store, gold, tier_a=tiers[0], tier_b=tiers[1])
        print(report.summary)
    finally:
        store.close()


def cmd_verify(args: argparse.Namespace) -> None:
    """Pre-flight health check for the data platform.

    Runs the verify skill (`axiom.extensions.builtins.data_platform.skills.verify`)
    against the live environment + DSN. Prints PASS/WARN/FAIL per check
    with operator-actionable remediation. Exits 0 on overall PASS,
    1 on FAIL.

    Closes the 'did I install this right?' loop tonight's stand-up
    surfaced over ~10 silent failure modes.
    """
    try:
        from axiom.extensions.builtins.data_platform.skills.verify import (
            Status, run_all_checks,
        )
    except ImportError:
        print("ERROR: data_platform extension not installed; "
              "try `pip install 'axiom-os-lm[data-platform]'`",
              file=sys.stderr)
        sys.exit(2)

    report = run_all_checks()

    icon = {Status.PASS: "✓", Status.WARN: "!", Status.FAIL: "✗"}
    for c in report.checks:
        print(f"  {icon[c.status]} {c.name:25s} {c.status.value:5s} {c.detail}")
        if c.status == Status.FAIL and c.remediation:
            print(f"      → {c.remediation}")

    print(f"\n{report.passed}/{report.total} passed", end="")
    if report.warned:
        print(f", {report.warned} warned", end="")
    if report.failed:
        print(f", {report.failed} FAILED", end="")
    print(f"\noverall: {report.overall.value}")

    sys.exit(0 if report.overall == Status.PASS else 1)


def cmd_generations(args: argparse.Namespace) -> None:
    """Show generation status per corpus."""
    from .store import ALL_CORPORA

    store = _get_store()
    try:
        from .generation import GenerationManager

        gm = GenerationManager(store)
        print("Corpus               Active  Candidate")
        print("-------------------  ------  ---------")
        for corpus in ALL_CORPORA:
            active = gm.get_active_generation(corpus)
            candidate = gm.get_candidate_generation(corpus)
            cand_str = str(candidate) if candidate else "-"
            print(f"{corpus:20s}  {active:>6d}  {cand_str:>9s}")
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Main / argument parser
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    _load_env()

    parser = argparse.ArgumentParser(prog="axiom.rag", description="RAG subsystem CLI")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    # index
    p_index = sub.add_parser("index", help="Index documents into the RAG store")
    p_index.add_argument("paths", nargs="*", help="Paths to index (default: repo docs/)")
    p_index.add_argument(
        "--corpus", default="rag-internal", help="Target corpus (default: rag-internal)"
    )

    # search
    p_search = sub.add_parser("search", help="Search the RAG index")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("-n", "--limit", type=int, default=5, help="Max results")

    # status
    sub.add_parser("status", help="Show per-corpus index statistics")

    # load-community
    p_lc = sub.add_parser("load-community", help="Load pre-built community corpus")
    p_lc.add_argument(
        "dump_path", nargs="?", default=None, help="Path to .sql dump (default: bundled)"
    )

    # sync
    p_sync = sub.add_parser("sync", help="Sync a corpus from remote source")
    p_sync.add_argument("target", choices=["org"], help="Corpus to sync")

    # watch
    p_watch = sub.add_parser("watch", help="Watch workspace dirs and re-index on change")
    p_watch.add_argument("--corpus", default="rag-internal", help="Target corpus")
    p_watch.add_argument("--quiet", action="store_true", help="Suppress startup output")

    # reindex
    p_reindex = sub.add_parser("reindex", help="Force full re-index of a corpus")
    p_reindex.add_argument("--corpus", default="rag-internal", help="Corpus to reindex")

    # upgrade (build new generation)
    p_upgrade = sub.add_parser("upgrade", help="Build a new RAG generation with semantic chunking")
    p_upgrade.add_argument("--source", required=True, help="Path to source documents")
    p_upgrade.add_argument("--corpus", default="rag-community", help="Target corpus")
    p_upgrade.add_argument("--tier", default="semantic", help="Chunking tier (fixed|semantic)")
    p_upgrade.add_argument("--no-backup", action="store_true", help="Skip pre-upgrade backup")

    # promote / rollback / discard
    p_promote = sub.add_parser("promote", help="Promote a candidate generation to active")
    p_promote.add_argument("generation", type=int, help="Generation number to promote")
    p_promote.add_argument("--corpus", default="rag-community")

    p_rollback = sub.add_parser("rollback", help="Rollback to a previous generation")
    p_rollback.add_argument("generation", type=int, help="Generation to rollback to")
    p_rollback.add_argument("--corpus", default="rag-community")

    p_discard = sub.add_parser("discard", help="Discard a candidate generation")
    p_discard.add_argument("generation", type=int, help="Generation to discard")
    p_discard.add_argument("--corpus", default="rag-community")

    # benchmark
    p_bench = sub.add_parser("benchmark", help="Run A/B quality benchmark")
    p_bench.add_argument(
        "--compare", nargs=2, metavar=("TIER_A", "TIER_B"), default=["fixed", "semantic"]
    )
    p_bench.add_argument("--corpus", default="rag-community")

    # generations
    sub.add_parser("generations", help="Show generation status per corpus")

    # ingest — durable, resumable ingest of a large corpus (spec-rag-ingest-advanced)
    p_ingest = sub.add_parser("ingest", help="Durable, resumable ingest of a large corpus")
    p_ingest.add_argument("paths", nargs="*", help="Files, directories, or globs to ingest")
    p_ingest.add_argument(
        "--corpus",
        default="rag-internal",
        choices=["rag-community", "rag-org", "rag-internal"],
    )
    p_ingest.add_argument("--target", default="local", help="'local' or a federation peer name")
    p_ingest.add_argument("--dry-run", action="store_true", help="Preflight only; no writes")
    p_ingest.add_argument("--resume", action="store_true", help="Resume from latest checkpoint")
    p_ingest.add_argument("--yes", action="store_true", help="Skip confirmation prompts")
    p_ingest.add_argument("--json", action="store_true", help="Machine-readable output")
    p_ingest.add_argument("--max-retries", type=int, default=5)
    p_ingest.add_argument("--checkpoint-dir", default=".axi/rag-ingest")
    p_ingest.add_argument("--calibration-sample", type=int, default=50)
    p_ingest.add_argument(
        "--rules", default=None, help="Path to a provenance rule TOML (exclude/quarantine/route)"
    )
    p_ingest.add_argument(
        "--no-rules",
        action="store_true",
        help="Acknowledge ingesting to a shared tier with no provenance rules",
    )

    # remove — one-shot deletion of a single document by name or full path
    p_add = sub.add_parser(
        "add", help="Add a single file to a corpus (operator one-shot)",
    )
    p_add.add_argument("path", help="Path to the file on disk to index")
    p_add.add_argument(
        "--corpus", default="rag-internal",
        choices=["rag-community", "rag-org", "rag-internal"],
    )
    p_add.add_argument(
        "--source-path", default=None,
        help="Override the recorded source path (e.g. box://CRISP/file.pdf "
             "for a local copy of a Box doc)",
    )
    p_add.add_argument(
        "--chunking-tier", default="fixed",
        choices=["fixed", "semantic"],
        help="Chunker strategy (default: fixed)",
    )

    p_remove = sub.add_parser(
        "remove",
        help="Remove a document (and all its chunks) by name or full path",
    )
    p_remove.add_argument(
        "name",
        help="Document basename or full source_path "
             "(e.g. \"1946 - CP2 Layer Details.pdf\" or \"box/CRISP/.../it.pdf\")",
    )
    p_remove.add_argument(
        "--corpus", default="rag-internal",
        choices=["rag-community", "rag-org", "rag-internal"],
    )
    p_remove.add_argument("--yes", action="store_true",
                          help="Confirm deletion (without this, prints a dry-run preview)")
    p_remove.add_argument("--all", action="store_true",
                          help="If the name matches multiple documents, delete all of them")

    # audit — find (and optionally purge) controlled content already in a corpus
    # eval — Qwen-with-RAG vs Qwen-baseline value-proof harness
    p_eval = sub.add_parser(
        "eval",
        help="Run the RAG eval harness against a YAML question set",
    )
    p_eval.add_argument("--questions", required=True,
                        help="YAML question set (see docs/working/rag-eval-nuclear-v0.yaml)")
    p_eval.add_argument("--no-retrieval", action="store_true",
                        help="Run baseline only (no retriever); skip the comparison")
    p_eval.add_argument("--retrieval-k", type=int, default=5,
                        help="Top-k chunks per query (default: 5)")
    p_eval.add_argument("--limit", type=int, default=None,
                        help="Limit to first N questions (for quick smokes)")

    sub.add_parser(
        "verify",
        help="Pre-flight health check (deps, OCR, Box auth, PG, schema)",
    )

    p_audit = sub.add_parser(
        "audit", help="Audit a corpus against provenance rules; --purge removes flagged docs"
    )
    p_audit.add_argument(
        "--corpus", default="rag-org", choices=["rag-community", "rag-org", "rag-internal"]
    )
    p_audit.add_argument("--rules", default=None, help="Path to a provenance rule TOML")
    p_audit.add_argument("--purge", action="store_true", help="Delete EXCLUDE-flagged documents")
    p_audit.add_argument("--yes", action="store_true", help="Skip the purge confirmation")

    p_stats = sub.add_parser("stats", help="[legacy] Alias for status")
    del p_stats  # suppress unused warning

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    dispatch = {
        "index": cmd_index,
        "ingest": cmd_ingest_advanced,
        "audit": cmd_audit,
        "add": cmd_add,
        "verify": cmd_verify,
        "remove": cmd_remove,
        "eval": cmd_eval,
        "search": cmd_search,
        "status": cmd_status,
        "stats": cmd_status,
        "load-community": cmd_load_community,
        "sync": cmd_sync,
        "reindex": cmd_reindex,
        "watch": cmd_watch,
        "upgrade": cmd_upgrade,
        "promote": cmd_promote,
        "rollback": cmd_rollback,
        "discard": cmd_discard,
        "benchmark": cmd_benchmark,
        "generations": cmd_generations,
    }

    if args.command in dispatch:
        dispatch[args.command](args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
