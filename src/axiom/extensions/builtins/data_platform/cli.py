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
        "--axiom-version", default="", help="axiom-os-lm pin (default: chart's appVersion)"
    )
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

    inst.add_argument(
        "--expose", default="ClusterIP", choices=["ClusterIP", "NodePort", "LoadBalancer"]
    )
    inst.add_argument("--node-port", type=int, default=0)
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
    # The REAL bronze→silver pass at a terminal (the peer of the Dagster conform
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
    return verb.replace("-", "_"), None


def _args_to_params(args: argparse.Namespace) -> dict[str, Any]:
    """Translate parsed args → skill params dict."""
    params: dict[str, Any] = {}
    for k, v in vars(args).items():
        if k in ("verb", "json", "kind", "dry_run"):
            continue
        if v is None:
            continue
        params[k.replace("-", "_")] = v

    if args.verb == "register" and args.kind:
        params["kind"] = args.kind
        provider = default_source_kind_registry().get(args.kind)
        try:
            params["kind_params"] = provider.params_from_args(args)
        except ValueError as exc:
            params["_kind_params_error"] = str(exc)

    if args.verb == "install":
        # Provider-driven helm values: the active DB + VectorStore
        # providers each contribute their --set pairs. The install
        # skill merges these into the helm invocation.
        params["_args_namespace"] = args  # the skill calls provider hooks
    return params


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
    if result.value is not None:
        if isinstance(result.value, dict) and "checks" in result.value:
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
    return _emit(result, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
