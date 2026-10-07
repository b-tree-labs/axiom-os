# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi data`` — the data-platform CLI surface.

**Provider-driven by construction.** The platform CLI names no:

- specific ingest source kind (Box, GDrive, …) — `SourceKindProvider`s
  attach their own flags to ``register <name> <kind>``.
- specific OLTP database (Postgres, MySQL, …) — `DatabaseKindProvider`s
  attach their own flags to ``install`` via ``--db-kind``.
- specific vector store (pgvector, Qdrant, …) — `VectorStoreProvider`s
  attach their own flags to ``install`` via ``--vector-kind``.

Adding a new provider of any of the three kinds ships a package +
import-time registration. NO platform-code change.

Per ADR-056: CLI verbs are thin wrappers that translate flags → params
dict and dispatch to ``SkillRegistry.invoke``. All business logic
lives in the skill functions.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from axiom.infra.paths import get_user_state_dir
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext, SkillResult

from . import skills as data_skills
from .database import default_database_kind_registry
from .gold_query import AGGREGATE_FNS, ANSWERABLE_TIERS
from .medallion import TIERS
from .sources import default_source_kind_registry
from .vectorstore import default_vector_store_registry

_PROG = "axi data"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=_PROG,
        description="data-platform: install, diagnose, ingest, manage connectors.",
    )
    p.add_argument("--json", action="store_true", help="emit the SkillResult as JSON")
    p.add_argument(
        "--actor",
        help="operator principal for the audit envelope "
        "(falls back to AXIOM_ACTOR env, then dev-mode "
        "hostname). e.g. '@operator:example-org'",
    )
    sub = p.add_subparsers(dest="verb", required=True)

    # ---- install (provider-driven) --------------------------------------
    db_registry = default_database_kind_registry()
    vec_registry = default_vector_store_registry()

    inst = sub.add_parser("install", help="Provision the data-platform on K8s.")
    inst.add_argument("--namespace", default="axiom-data")
    inst.add_argument("--release", default="axiom-data-platform")
    inst.add_argument("--kube-context", help="kubectl context (default: current)")
    inst.add_argument(
        "--db-kind",
        default="postgres",
        choices=db_registry.kinds(),
        help="OLTP database kind (provider). Default: postgres.",
    )
    inst.add_argument(
        "--db-mode",
        default="internal",
        choices=["internal", "external"],
        help="`internal` deploys the bundled DB; `external` uses --db-dsn.",
    )
    inst.add_argument("--db-dsn", default="", help="connect string when --db-mode=external")
    inst.add_argument(
        "--vector-kind",
        default="pgvector",
        choices=vec_registry.kinds(),
        help="vector-store kind (provider). Default: pgvector "
        "(co-locates with --db-kind=postgres).",
    )
    inst.add_argument(
        "--vector-dsn", default="", help="vector-store connect string when not co-located"
    )
    # Each DB + VectorStore provider attaches its own kind-specific flags.
    # We attach ALL providers' flags so help discovers everything; the
    # active provider is selected at runtime from --db-kind / --vector-kind.
    for k in db_registry.kinds():
        db_registry.get(k).add_install_args(inst)
    for k in vec_registry.kinds():
        vec_registry.get(k).add_install_args(inst)

    inst.add_argument("--bronze-size", default="100Gi")
    inst.add_argument(
        "--rules", help="path to provenance rules TOML (optional; default = quarantine all)"
    )
    inst.add_argument("--dry-run", action="store_true")
    inst.add_argument(
        "--skip-diagnose", action="store_true", help="don't auto-invoke data.diagnose post-install"
    )

    # ---- diagnose --------------------------------------------------------
    diag = sub.add_parser("diagnose", help="Post-install health checks.")
    diag.add_argument("--namespace", default="axiom-data")
    diag.add_argument("--release", default="axiom-data-platform")
    diag.add_argument("--kube-context")

    # ---- troubleshoot ----------------------------------------------------
    # Peer of `diagnose`: diagnose says whether it is healthy, troubleshoot
    # says why it is not. It has declared a `cli` surface since it was
    # registered; this is the parser that makes that true.
    ts = sub.add_parser(
        "troubleshoot",
        help="Explain why a connector is failing, with the fix.",
    )
    ts.add_argument("--connector", help="one connector (default: every registered one)")

    # ---- ingest ----------------------------------------------------------
    ing = sub.add_parser("ingest", help="Gated source → bronze → RAG pass.")
    ing.add_argument("--connector", required=True)
    ing.add_argument("--since")
    ing.add_argument("--volume-mode", default="confirm", choices=["off", "refuse", "confirm"])
    ing.add_argument(
        "--workers",
        type=int,
        help="concurrent fetch/extract workers (default 1; "
        "also DP1_INGEST_WORKERS env). The bottleneck is "
        "per-item source fetch + OCR, not the embedder.",
    )

    # ---- refresh ---------------------------------------------------------
    ref = sub.add_parser(
        "refresh",
        help="Incremental (CDC) delta ingest — derive a watermark off the "
        "last indexed time and pull only what changed since.",
    )
    ref.add_argument("--connector", required=True)
    ref.add_argument(
        "--overlap-minutes",
        type=int,
        default=60,
        help="back the watermark off by this many minutes so "
        "items written around the last run aren't missed "
        "(default 60).",
    )

    # ---- register <name> <kind> [kind-specific flags] -------------------
    reg = sub.add_parser(
        "register",
        help="Register a connector — `register <name> <kind> [flags]`.",
    )
    reg.add_argument("name", help="connector name")
    reg.add_argument("--bronze-root", required=True)
    reg.add_argument(
        "--site",
        help="site this connector's rows belong to (e.g. site-a). Conformance "
        "needs it to attribute rows to silver.signals; a connector registered "
        "without a site is unattributable and its rows never leave bronze.",
    )
    reg.add_argument("--rag-dsn-env", default="DP1_RAG_DSN")
    reg.add_argument("--provenance-rules-file")
    reg.add_argument(
        "--default-disposition", default="quarantine", choices=["allow", "quarantine", "exclude"]
    )
    reg.add_argument("--default-tier", default="rag-community")
    reg.add_argument(
        "--credential-ref",
        help="SecretRef URL resolving to this connector's credential "
        "(e.g. a DSN for sql-tabular: env://SHADOW_DB_DSN or "
        "openbao://host/db/dsn). Resolved via the secrets extension.",
    )
    reg.add_argument(
        "--promotion-map-file",
        help="path to a declared bronze->gold promotion map (TOML: "
        "column map + optional EAV pivot + source precedence + "
        "SCD-2). Validated at register time.",
    )
    reg.add_argument("--force", action="store_true")
    reg.add_argument(
        "--allow-unmapped",
        action="store_true",
        help="register a push connector WITHOUT a site anyway. Without a site "
        "a push connector accepts rows that conformance silently skips "
        "forever, so registration refuses by default; this flag is the "
        "explicit escape hatch for a deliberately unattributed sink.",
    )
    kind_sub = reg.add_subparsers(
        dest="kind",
        required=True,
        metavar="kind",
        help="source kind — each registers its own flags",
    )
    source_registry = default_source_kind_registry()
    for kind_name in source_registry.kinds():
        provider = source_registry.get(kind_name)
        kp = kind_sub.add_parser(kind_name, help=provider.description)
        provider.add_register_args(kp)

    # ---- ingest-freshness ------------------------------------------------
    fresh = sub.add_parser(
        "ingest-freshness",
        help="Did rows LAND? Per-feed freshness against expectations — the "
        "effect-side counterpart of `refresh` (which asks whether the "
        "pull RAN). A connector can refresh on time and deliver nothing; "
        "this verb is how that case surfaces.",
    )
    fresh.add_argument("--site", help="one site (all sites when omitted)")
    fresh.add_argument(
        "--expect-hours",
        type=float,
        help="expected maximum age in hours, applied to every feed",
    )

    # ---- enroll ----------------------------------------------------------
    enr = sub.add_parser(
        "enroll",
        help="Enroll a source from its manifest — `enroll source.toml --bronze-root …`.",
    )
    enr.add_argument("manifest", help="path to the source manifest (source.toml)")
    enr.add_argument("--bronze-root", required=True)
    enr.add_argument(
        "--default-disposition",
        default="allow",
        choices=["allow", "quarantine", "exclude"],
    )

    # ---- unregister ------------------------------------------------------
    unreg = sub.add_parser("unregister", help="Remove a connector.")
    unreg.add_argument("name", help="connector name")

    # ---- list ------------------------------------------------------------
    # ---------------------------------------------------------------- gold
    #
    # The read verbs a researcher actually wants, on the CLI for the first
    # time. They dispatch to the same skills MCP calls — per ADR-056 a CLI verb
    # is a thin wrapper, and the tier guard lives in the skill, so a shell is
    # not a way around it.
    def _gold(name: str, help_: str) -> argparse.ArgumentParser:
        g = sub.add_parser(name, help=help_)
        g.add_argument(
            "--format",
            default="text",
            choices=["text", "json", "csv", "ndjson"],
            help="how to write the answer (default: text)",
        )
        g.add_argument("--dsn", help="database to read; defaults to the node's")
        g.add_argument(
            "--tiers",
            nargs="+",
            help="access tiers to read. Honoured only for an assured principal — "
            "a caller cannot widen its own tiers, which is what makes the guard "
            "more than decoration.",
        )
        return g

    _gold("tables", "List the tables and views in the gold tier.")

    # `describe`, `series` and `aggregate` are registered with the medallion
    # verbs below, through `_gold`, so they take these same output flags. One
    # parser per verb: argparse refuses a second, and two would drift anyway.

    _gold("roles", "Which quantities the fleet can answer, and which sites answer each.")

    _gold(
        "serving-report",
        "Who is using the serving tier and who met a limit (ADR-157).",
    )

    gc = _gold("compare", "The same quantity across sites.")
    gc.add_argument("--role", required=True, help="the quantity to compare")
    gc.add_argument("--from", dest="start", help="ISO instant, inclusive")
    gc.add_argument("--to", dest="end", help="ISO instant, exclusive")

    # `axi data retrieval …` — a named question, asked from any surface. The
    # parent noun groups them so `axi data --help` stays readable; each verb is
    # still one skill, declared in the manifest, per ADR-056.
    retr = sub.add_parser(
        "retrieval",
        help="Name a retrieval once and ask it from any surface.",
    )
    rsub = retr.add_subparsers(dest="retrieval_verb", required=True)

    rsave = rsub.add_parser("save", help="Name a retrieval (or replace one).")
    rsave.add_argument("name")
    rsave.add_argument("--site", default="")
    rsave.add_argument("--feed", default="")
    rsave.add_argument(
        "--channel", action="append", dest="channels", default=[],
        help="repeatable; a retrieval naming no channel names no data",
    )
    rsave.add_argument(
        "--window", default="",
        help="a SPAN (24h, 7d, all, operating), or from/to for one fixed period, "
             "or omit it and say when at call time",
    )
    rsave.add_argument("--bucket", default="")
    rsave.add_argument("--note", default="")
    rsave.add_argument("--replace", action="store_true")

    rsub.add_parser("list", help="Every saved retrieval.")

    rshow = rsub.add_parser("show", help="One saved retrieval.")
    rshow.add_argument("name")

    rrm = rsub.add_parser("rm", help="Forget a saved retrieval.")
    rrm.add_argument("name")

    rdia = rsub.add_parser(
        "dialect",
        help="The query parameters a named surface actually reads.",
    )
    rdia.add_argument("name")
    rdia.add_argument(
        "dialect",
        help="chart or telemetry — the same window is spelled differently by "
             "each, and neither rejects the other's spelling",
    )

    lst = sub.add_parser("list", help="List registered resources.")
    lst.add_argument(
        "resource",
        nargs="?",
        default="connectors",
        choices=["connectors", "kinds", "db-kinds", "vector-kinds"],
        help="resource to list (default: connectors)",
    )

    # ---- preflight -------------------------------------------------------
    pre = sub.add_parser(
        "preflight",
        help="Live-verify a connector's auth + access; print fixes for anything wrong.",
    )
    pre.add_argument("name", help="connector name")

    # ---- conform-try -----------------------------------------------------
    # Maps to the data.conform_try skill (main() converts the hyphen). Writes
    # nothing: it calls a registered normalizer on a record you supply and
    # reports what silver would have absorbed silently.
    ctry = sub.add_parser(
        "conform-try",
        help="Dry-run a normalizer over one bronze record; report the rows and the traps.",
    )
    ctry.add_argument(
        "--schema-ref",
        dest="schema_ref",
        help="Which normalizer to exercise. Omit to list what is registered.",
    )
    ctry.add_argument(
        "--record",
        help="The bronze record as JSON, payload under 'row'. Use @path to read a file.",
    )

    # ---- the tenant data kit -------------------------------------------
    # Five verbs, one skill each (data.kit_*). See prd-tenant-data-kit.md.
    kinit = sub.add_parser("kit-init", help="Write a data kit with one working example of each contribution.")
    kinit.add_argument("--tenant", required=True, help="your site id on the shared platform, e.g. rig-site")
    kinit.add_argument("--dir", help="the site repository (default: here)")
    kinit.add_argument("--force", action="store_true", help="write beside files already in data/")
    for verb, about in (("kit-up", "Start the local medallion for this kit."),
                        ("kit-try", "Run every tier on local data and show each one."),
                        ("kit-down", "Remove the local medallion.")):
        kp = sub.add_parser(verb, help=about)
        kp.add_argument("--dir", help="the site repository (default: here)")
    kchk = sub.add_parser("kit-check", help="The gate before promotion: declarations, lint, isolation proof.")
    kchk.add_argument("--dir", help="the site repository (default: here)")
    kchk.add_argument("--static-only", dest="static_only", action="store_true",
                      help="skip the isolation proof (for CI without Docker); it is reported, not passed")

    # ---- conform ---------------------------------------------------------
    # The flag spelling of the same thing. See _SKILL_ALIASES: both reach one
    # skill, and a test asserts they produce identical output.
    conf = sub.add_parser(
        "conform",
        help="Bronze to canonical silver. --dry-run exercises one record and writes nothing.",
    )
    conf.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry-run a normalizer over one record; identical to 'conform-try'.",
    )
    conf.add_argument("--schema-ref", dest="schema_ref", help="Which normalizer to exercise.")
    conf.add_argument(
        "--record",
        help="The bronze record as JSON, payload under 'row'. Use @path to read a file.",
    )

    # ---- conform-run -----------------------------------------------------
    # The REAL bronze→silver pass at a terminal (the peer of the scheduled conform
    # asset). Site comes from the connector registry; the funnel is reported
    # loudly and a skip/unknown makes it exit non-zero — a systemd timer's entry.
    crun = sub.add_parser(
        "conform-run",
        help="Run the real bronze→silver conform pass over all connectors; upsert to silver.",
    )
    crun.add_argument(
        "--bronze-root",
        dest="bronze_root",
        help="bronze root to conform (default: ~/.axi/bronze).",
    )
    crun.add_argument("--dsn", help="database DSN (default: DP1_RAG_DSN / DATABASE_URL).")
    crun.add_argument(
        "--batch-size",
        dest="batch_size",
        type=int,
        help="rows per round trip (default 1000). One statement per row makes "
        "the wire time the run time.",
    )
    crun.add_argument(
        "--checkpoint-rows",
        dest="checkpoint_rows",
        type=int,
        help="rows between commits (default 50000). One transaction for the "
        "whole pass pins the xmin horizon, so autovacuum reclaims nothing "
        "while it runs.",
    )
    crun.add_argument(
        "--no-strict",
        dest="strict",
        action="store_false",
        default=True,
        help="do not exit non-zero on a skipped connector or unknown schema (still reported).",
    )

    # ---- activity --------------------------------------------------------
    act = sub.add_parser(
        "activity",
        help="What the RAG ingested per connector since a time ago (e.g. --since 24h | 7d | 90m).",
    )
    act.add_argument(
        "--since", default="24h", help="lookback window: 24h, 7d, 90m, 2w (default 24h)"
    )
    act.add_argument("--connector", help="filter to one connector")

    # ---- backfill-urls ---------------------------------------------------
    bf = sub.add_parser(
        "backfill-urls",
        help="Hydrate documents.source_url/source_ref_id for a connector by "
        "re-cataloging its source (ADR-091). Use --dry-run first.",
    )
    bf.add_argument("--connector", required=True)
    bf.add_argument(
        "--dry-run", action="store_true", help="report the match rate without writing anything"
    )
    bf.add_argument("--corpus", help="corpus to backfill (default: the connector's default tier)")

    # ---- gold-* ----------------------------------------------------------
    # ADR-115 generic medallion answering. These four were registered as
    # skills with `cli` in their declared surfaces and no parser behind it,
    # so the surface existed on paper and nowhere else.
    # One introspection vocabulary for every tier. The tier is an ARGUMENT,
    # not part of the verb name: a caller should not have to know which
    # medallion they are on before they can phrase the question.
    cat = sub.add_parser("catalog", help="What a medallion tier holds.")
    cat.add_argument("--tier", choices=sorted(TIERS), help="bronze | silver | gold (default gold)")
    cat.add_argument("--object", help="one connector, on bronze")

    desc = _gold("describe", "The shape of one object in a tier.")
    desc.add_argument("--tier", choices=sorted(TIERS), help="default: gold")
    desc.add_argument(
        "--object", "--table", dest="object", required=True, help="the table to describe"
    )

    fresh = sub.add_parser(
        "freshness",
        help="What has stopped advancing. Bronze says whether a PRODUCER is "
        "still pushing; silver whether the CONFORM PASS is advancing.",
    )
    fresh.add_argument("--tier", choices=sorted(TIERS), help="default: gold")
    fresh.add_argument("--site", help="one site (silver/gold)")

    samp = sub.add_parser(
        "sample", help="Some records, to see the shape of what is arriving. Bronze only."
    )
    samp.add_argument("--tier", choices=sorted(TIERS), help="default: gold")
    samp.add_argument("--object", required=True, help="the connector")
    samp.add_argument("--day", help="one day directory, e.g. 2026-09-28")
    samp.add_argument("--limit", type=int)

    def _answering_args(parser):
        """The arguments that make an answer say what population it summarised."""
        parser.add_argument("--table", required=True)
        parser.add_argument("--column", required=True)
        parser.add_argument("--filter", help="e.g. \"site = 'site-a' and channel = 'temp'\"")
        parser.add_argument("--start", "--from", dest="start", help="window start (ISO timestamp)")
        parser.add_argument(
            "--end", "--to", dest="end", help="window end, exclusive (ISO timestamp)"
        )
        parser.add_argument("--time-column", help="the column the window applies to")
        parser.add_argument(
            "--group-by",
            help="split the answer, comma-separated — 'source_class' gives you "
            "measured against predicted instead of one number over both",
        )
        parser.add_argument(
            "--allow-mixed",
            action="store_true",
            help="summarise across units or source classes anyway; say so on purpose",
        )
        parser.add_argument(
            "--allow-mixed-units",
            action="store_true",
            default=None,
            help="the unit labels differ but the rows share a scale; source "
            "classes are still kept apart",
        )
        parser.add_argument(
            "--include-synthetic",
            action="store_true",
            default=None,
            help="include the install's own self-test rows",
        )
        parser.add_argument(
            "--tier",
            choices=sorted(ANSWERABLE_TIERS),
            help="which served tier to answer from (default gold). A silver "
            "answer says so: its rows are conformed but not projected.",
        )

    ga = _gold(
        "aggregate",
        "One deterministic aggregate of a gold column. Refuses to blend "
        "units or source classes into a single number — use --group-by.",
    )
    _answering_args(ga)
    ga.add_argument("--fn", required=True, choices=sorted(AGGREGATE_FNS))

    gs = _gold(
        "series",
        "A bucketed series from a gold table. With --group-by "
        "source_class, one line per population — the measured-vs-predicted overlay.",
    )
    _answering_args(gs)
    gs.add_argument("--bucket", required=True, help="interval, e.g. '1 hour' or '15 minutes'")
    gs.add_argument("--fn", default="mean", choices=sorted(AGGREGATE_FNS))

    uc = sub.add_parser(
        "uncertainty-coverage",
        help="How much of the served surface can say how well it is known: "
        "per site and stream, points with declared sources, magnitude only, or neither.",
    )
    uc.add_argument("--site", help="narrow to one site")
    uc.add_argument(
        "--include-sources",
        action="store_true",
        default=None,
        help="also list the declared uncertainty sources",
    )

    # ---- backup ----------------------------------------------------------
    bak = sub.add_parser(
        "backup",
        help="Policy-driven database backup (custom-format pg_dump + "
        "retention prune + optional off-box replica).",
    )
    bak.add_argument("--dsn", help="database DSN (default: DP1_RAG_DSN / DATABASE_URL)")
    bak.add_argument("--label", default="", help="label appended to the filename")
    bak.add_argument("--target-root", help="artifact directory (default: BackupPolicy.target_root)")
    bak.add_argument(
        "--box-secret-ref",
        help="SecretRef for Box auth when the policy's offbox leg "
        "is enabled (default env://BOX_JWT_CONFIG)",
    )

    # ---- ensure-schema ---------------------------------------------------
    ens = sub.add_parser(
        "ensure-schema",
        help="Bring the conformance tier's schema up to the installed code "
        "(silver.signals columns + gold views). Reports what it added.",
    )
    ens.add_argument("--dsn", help="database DSN (default: DP1_RAG_DSN / DATABASE_URL)")
    ens.add_argument(
        "--lock-timeout",
        help="how long a DDL statement may wait for its lock (default 5s) — short so a "
        "deploy never queues in front of readers",
    )

    # ---- rederive --------------------------------------------------------
    red = sub.add_parser(
        "rederive",
        help="Apply a site's declarations to rows already written — a unit "
        "filled in later reaches its own history. Counts only unless --apply.",
    )
    red.add_argument("--site", help="one site, or comma-separated (default: every site)")
    red.add_argument(
        "--apply", action="store_true",
        help="write the change. Without it everything runs and is rolled back, "
        "so the reported count is exact rather than predicted",
    )
    red.add_argument("--bronze-root", help="bronze location (default ~/.axi/bronze)")
    red.add_argument("--dsn", help="database DSN (default: DP1_RAG_DSN / DATABASE_URL)")

    # ---- gaps ------------------------------------------------------------
    gap = sub.add_parser(
        "gaps",
        help="What this site would need to declare, worst first. Names the act "
        "that closes each gap and what closing it would do, in rows.",
    )
    gap.add_argument("--site", required=True, help="the site to assess")
    gap.add_argument("--dsn", help="database DSN (default: DP1_RAG_DSN / DATABASE_URL)")
    gap.add_argument(
        "--collisions", action="store_true",
        help="also look for timestamp collisions. Off by default because it "
        "groups every row the site has",
    )

    # ---- tier-audit ------------------------------------------------------
    # The static guard holds the code to ADR-128. This is the half that can
    # see the database, which is where the drift actually showed up.
    aud = sub.add_parser(
        "tier-audit",
        help="Report the medallion's actual shape and anything ADR-128 did not "
        "expect: an undeclared base table in gold, a gold view serving bronze.",
    )
    aud.add_argument("--dsn", help="database DSN (default: DP1_RAG_DSN / DATABASE_URL)")
    aud.add_argument(
        "--schemas",
        help="comma-separated tiers to read (default bronze,silver,gold)",
    )

    # ---- backup-policy ---------------------------------------------------
    # Arming the nightly schedule is done by editing the policy file, so the
    # arming gesture and the configuration are the same gesture. This is the
    # step in between: what the node LOADED, not what the file says.
    bpol = sub.add_parser(
        "backup-policy",
        help="Show the backup policy as the node actually loads it, and flag "
        "any key in the file the loader silently dropped.",
    )
    bpol.add_argument(
        "--state-dir", help="policy location (default: the node's ~/.axi/plinth)"
    )
    bpol.add_argument(
        "--apply",
        action="store_true",
        help="project the policy onto PULSE now. Without this, editing the policy "
        "file changes nothing until the orchestrator restarts — the cadences are "
        "only synced at startup, so `enabled = true` alone arms nothing.",
    )

    # ---- backup-validate -------------------------------------------------
    bval = sub.add_parser(
        "backup-validate",
        help="Prove the newest backup exists, is fresh, and is restorable "
        "(pg_restore TOC; optional live scratch restore).",
    )
    bval.add_argument(
        "--target-root", help="artifact directory (default: BackupPolicy.target_root)"
    )
    bval.add_argument(
        "--max-age-hours", type=float, help="staleness threshold in hours (default 26)"
    )
    bval.add_argument(
        "--validate-restore",
        action="store_true",
        help="restore into a scratch database + row-count sanity",
    )
    bval.add_argument("--scratch-dsn", help="disposable database DSN for --validate-restore")
    bval.add_argument(
        "--dsn",
        help="live DSN for the row-count comparison (default: "
        "DP1_RAG_DSN / DATABASE_URL is NOT assumed — "
        "comparison is skipped without it)",
    )

    return p


def _build_ctx() -> SkillContext:
    return SkillContext(
        registry=data_skills.bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.data"),
        user_prompt=_terminal_prompt if sys.stdin.isatty() else None,
    )


def _terminal_prompt(prompt: str) -> str:
    return input(prompt)


#: Verb spellings that resolve to one skill.
#:
#: The convention, so adding the next synonym is a line rather than a decision:
#: a hyphenated compound verb (``conform-try``) and a flagged base verb
#: (``conform --dry-run``) may both reach one skill, and neither is "the real
#: one". They are **aliases, not implementations** — resolution happens here,
#: once, in this table, and every spelling is asserted to produce byte-identical
#: output by ``test_every_spelling_reaches_one_skill``.
#:
#: Key is ``(verb, flag)``; a ``None`` flag matches the bare verb. Everything
#: not listed maps to its own name with hyphens converted, which is the default
#: and covers most verbs.
_SKILL_ALIASES: dict[tuple[str, str | None], str] = {
    ("conform", "dry_run"): "conform_try",
    # `ingest-freshness` shipped before the medallion vocabulary was uniform.
    # It answers exactly `freshness --tier silver`, so it resolves there
    # rather than being a second implementation. Kept because it is released:
    # renaming an unreleased verb is free, breaking a released one is not.
    ("ingest-freshness", None): "freshness",
}

#: Friendly spellings for the gold read verbs.
#:
#: The skills are named `gold_series`, `gold_tables` and so on because they say
#: which tier they read. At a terminal that prefix is noise: somebody pulling a
#: series types `axi data series`, and `axi data gold-series` resolves to the
#: same skill for anybody who wants to be explicit.
#:
#: These are the verbs a researcher reaches for, and until now NONE of them was
#: reachable from a shell — they existed as skills, so MCP had them and a
#: terminal did not.
_GOLD_VERBS: dict[str, str] = {
    "tables": "catalog",
    "describe": "describe",
    "series": "series",
    "aggregate": "aggregate",
    "roles": "roles",
    "compare": "compare",
    "serving-report": "serving_report",
}

#: Verbs that only exist in a flagged form today, with the reason.
#:
#: ``conform`` without ``--dry-run`` would be the real bronze-to-silver pass,
#: which runs in the pipeline rather than at a terminal. Saying so beats a
#: confusing argparse error, and beats silently doing the dry run.
_FLAG_ONLY: dict[str, str] = {
    "conform": (
        "axi data conform is the dry-run door. Use --dry-run to exercise one "
        "record against a registered normalizer (identical to 'axi data "
        "conform-try'), or 'axi data conform-run' for the real bronze→silver pass."
    ),
}


def resolve_skill(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """Map a parsed verb, plus any mode flag, onto one skill name.

    Returns ``(skill_name, error)``. Exactly one is populated.
    """
    verb = args.verb
    for (alias_verb, flag), skill in _SKILL_ALIASES.items():
        if verb == alias_verb and flag is not None and getattr(args, flag, False):
            return skill, None
    if verb in _FLAG_ONLY:
        return None, _FLAG_ONLY[verb]
    if verb in _GOLD_VERBS:
        return _GOLD_VERBS[verb], None
    # `gold-series` is the explicit spelling of `series`, for anybody who wants
    # to say which tier they mean.
    if verb.startswith("gold-") and verb[len("gold-"):] in _GOLD_VERBS:
        return _GOLD_VERBS[verb[len("gold-"):]], None
    # A compound noun with its own sub-verbs. `axi data retrieval save` is the
    # skill `retrieval_save`, so the parent noun keeps `axi data --help`
    # readable while each verb stays one skill per ADR-056.
    sub = getattr(args, f"{verb}_verb", None)
    if sub:
        return f"{verb}_{str(sub).replace('-', '_')}", None
    return verb.replace("-", "_"), None


def _args_to_params(args: argparse.Namespace) -> dict[str, Any]:
    """Translate parsed args → skill params dict."""
    params: dict[str, Any] = {}
    for k, v in vars(args).items():
        # `<noun>_verb` is how argparse hands back a sub-verb; it selects the
        # skill and is not an argument to it.
        if k in ("verb", "json", "kind", "dry_run") or k.endswith("_verb"):
            continue
        if v is None:
            continue
        params[k.replace("-", "_")] = v

    if args.verb in _GOLD_VERBS:
        # `--from/--to` are two flags at a terminal and one window to the skill,
        # which already knows how to bound a query by a column. `--format` is
        # the CLI's own business and never reaches the skill.
        params.pop("format", None)
    if args.verb in _GOLD_VERBS and args.verb not in ("aggregate", "series"):
        start, end = params.pop("start", None), params.pop("end", None)
        if start or end:
            params["window"] = {
                "column": params.get("time_column", "ts"),
                "start": start,
                "end": end,
            }

    if args.verb == "register" and args.kind:
        params["kind"] = args.kind
        provider = default_source_kind_registry().get(args.kind)
        try:
            params["kind_params"] = provider.params_from_args(args)
        except ValueError as exc:
            params["_kind_params_error"] = str(exc)

    if args.verb in ("aggregate", "series"):
        # --start/--end/--time-column are one window to the skill. Assembling
        # it here keeps the skill's contract a dict and the terminal's three
        # flags, instead of asking a human to type JSON.
        start, end = params.pop("start", None), params.pop("end", None)
        time_column = params.pop("time_column", None)
        if start or end:
            params["window"] = {"column": time_column, "start": start, "end": end}
        elif time_column:
            params["time_column"] = time_column
        if args.verb == "series" and time_column:
            params["time_column"] = time_column

    if args.verb == "install":
        # Provider-driven helm values: the active DB + VectorStore
        # providers each contribute their --set pairs. The install
        # skill merges these into the helm invocation.
        params["_args_namespace"] = args  # the skill calls provider hooks
    return params


def _unit_of(value: Any) -> str:
    """The unit an envelope declares, or "" when it declares none.

    Never a guess. A column with no declared unit exports without one and the
    header says so by omission — inventing `degC` because the numbers look like
    temperatures is the mistake this platform spent a day undoing.
    """
    if not isinstance(value, dict):
        return ""
    unit = value.get("unit")
    if unit is None and isinstance(value.get("provenance"), dict):
        unit = value["provenance"].get("unit")
    if isinstance(unit, dict):
        return str(unit.get("value") or "")
    return str(unit or "")


def _writes_records(args: argparse.Namespace) -> bool:
    """Whether this invocation's `--format` means "how to write the answer".

    Only for the gold read verbs. See the note at the call site: the flag is
    already spoken for elsewhere in this same CLI.
    """
    if args.verb not in _GOLD_VERBS:
        return False
    return getattr(args, "format", "text") in ("csv", "ndjson")


def _note_of(value: Any) -> str:
    """Why a verb returned nothing, in its own words.

    Under `provenance`, which is where the envelope keeps what it did and why —
    not at the top level, where an earlier version of this looked and found
    nothing.
    """
    if not isinstance(value, dict):
        return ""
    prov = value.get("provenance")
    if isinstance(prov, dict) and prov.get("note"):
        return str(prov["note"])
    return str(value.get("note") or "")


def _rows_of(value: Any) -> tuple[list[dict[str, Any]], str]:
    """The record list inside an envelope, and what it is called.

    One shape per verb, so the writer below does not have to know which verb it
    is serving: a series has `series`, the rest return a list of rows.
    """
    data = (value or {}).get("data") if isinstance(value, dict) else None
    if isinstance(data, dict):
        for key in ("series", "rows", "tables", "roles", "columns"):
            if isinstance(data.get(key), list):
                return data[key], key
        return [data], "row"
    if isinstance(data, list):
        return data, "rows"
    return [], "rows"


def _write_records(result: SkillResult, fmt: str) -> int:
    """CSV or NDJSON of whatever the verb returned.

    The unit travels in the value column's NAME for CSV — `value_degC`, the
    same convention the figure export uses — and in every record for NDJSON,
    because a stream has no header to carry it.
    """
    import csv as _csv

    if not result.ok:
        for err in result.errors:
            print(err, file=sys.stderr)
        return result.exit_code

    rows, _kind = _rows_of(result.value)
    unit = _unit_of(result.value)
    if not rows:
        # A refusal is not an empty file, and it is not a success.
        #
        # The reason lives in the envelope's PROVENANCE, which is where the
        # good sentences are: "this answer spans 5 units (W, console_units,
        # degC, mol, pct) and rows declaring none … Narrow the filter, or pass
        # allow_mixed_units". Reading `note` off the top level printed "no
        # rows" instead and threw that away.
        #
        # Non-zero, because the contract a script needs is "zero means there
        # is data in that file". A cron job that redirects stdout and chains on
        # `&&` would otherwise carry on with an empty file and report nothing.
        print(_note_of(result.value) or "no rows matched", file=sys.stderr)
        return 1

    named = [
        {(f"{k}_{unit}" if k == "value" and unit else k): v for k, v in row.items()}
        for row in rows
    ]
    if fmt == "ndjson":
        for row, original in zip(named, rows, strict=True):
            out = dict(original)
            if unit:
                out["unit"] = unit
            print(json.dumps(out, default=str))
        return 0
    writer = _csv.DictWriter(sys.stdout, fieldnames=list(named[0].keys()))
    writer.writeheader()
    writer.writerows(named)
    return 0


def _emit(result: SkillResult, as_json: bool) -> int:
    if as_json:
        print(
            json.dumps(
                {
                    "ok": result.ok,
                    "value": result.value,
                    "errors": result.errors,
                    "actions_taken": result.actions_taken,
                },
                indent=2,
                default=str,
            )
        )
        return result.exit_code
    for action in result.actions_taken:
        print(f"• {action}")
    if isinstance(result.value, dict) and isinstance(result.value.get("text"), str):
        # A verb that renders its own report (the kit verbs) says so by carrying
        # `text`; the structured fields are still there under --json.
        print(result.value["text"])
    elif result.value is not None:
        # The connector-preflight pretty rendering fires only on ITS shape
        # (connector + kind + ok-style checks). Other verbs also return a
        # "checks" list (backup_validate: name/status/detail) and must fall
        # through to the generic renderers — a printer that crashes after
        # the work succeeded hides the effect it was asked to report.
        if (
            isinstance(result.value, dict)
            and "checks" in result.value
            and "connector" in result.value
            and "kind" in result.value
        ):
            v = result.value
            print(f"Connector: {v['connector']} ({v['kind']})")
            for c in v["checks"]:
                mark = "✓" if c["ok"] else "✗"
                print(f"  {mark} {c['name']}: {c['message']}")
                if not c["ok"] and c["remediation"]:
                    who = "(admin) " if c.get("actor") == "admin" else ""
                    print(f"      → {who}{c['remediation']}")
                    if c.get("copy_value"):
                        print(f"        copy: {c['copy_value']}")
            print(
                "\n  All good — ingestion will run on the next sensor tick."
                if v["ok"]
                else "\n  Fix the items above, then re-run preflight."
            )
            return result.exit_code
        if isinstance(result.value, dict) and "items" in result.value:
            for item in result.value["items"]:
                print("  " + "  ".join(f"{k}={v}" for k, v in item.items()))
        elif isinstance(result.value, (str, int, float, bool)):
            print(result.value)
        else:
            print(json.dumps(result.value, indent=2, default=str))
    if not result.ok:
        for err in result.errors:
            print(f"ERROR: {err}", file=sys.stderr)
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    ctx = _build_ctx()
    params = _args_to_params(args)
    record = params.get("record")
    if isinstance(record, str) and record.startswith("@"):
        # @path reads the record from a file, so a fixture can be piped straight
        # from CI without shell-quoting a JSON blob.
        try:
            params["record"] = Path(record[1:]).expanduser().read_text()
        except OSError as exc:
            return _emit(
                SkillResult(ok=False, errors=[f"cannot read {record[1:]}: {exc}"]), args.json
            )
    if params.pop("_kind_params_error", None):
        return _emit(
            SkillResult(ok=False, errors=[params["_kind_params_error"]]),
            args.json,
        )
    # One resolver for every spelling: hyphenated compounds map to snake_case,
    # and _SKILL_ALIASES redirects a flagged base verb onto the same skill.
    skill_name, alias_error = resolve_skill(args)
    if alias_error is not None:
        return _emit(SkillResult(ok=False, errors=[alias_error]), args.json)
    result = invoke_capability(
        ctx.registry,
        f"data.{skill_name}",
        params,
        ctx,
        surface=CLI_SURFACE,
    )
    # `--format` means different things to different verbs, and only the gold
    # read verbs mean "how to write the answer" by it. `register … --format csv`
    # is the TABULAR SOURCE's file format and has been since before this; reading
    # the flag globally sent a register result through the records writer, which
    # printed nothing and returned 1.
    fmt = getattr(args, "format", "text") if args.verb in _GOLD_VERBS else "text"
    if _writes_records(args):
        return _write_records(result, fmt)
    return _emit(result, args.json or fmt == "json")


if __name__ == "__main__":
    raise SystemExit(main())
