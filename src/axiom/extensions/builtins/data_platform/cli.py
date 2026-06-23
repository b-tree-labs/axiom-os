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
from typing import Any

from axiom.infra.paths import get_user_state_dir
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
    p.add_argument("--json", action="store_true",
                   help="emit the SkillResult as JSON")
    p.add_argument("--actor",
                   help="operator principal for the audit envelope "
                        "(falls back to AXIOM_ACTOR env, then dev-mode "
                        "hostname). e.g. '@operator:example-org'")
    sub = p.add_subparsers(dest="verb", required=True)

    # ---- install (provider-driven) --------------------------------------
    db_registry = default_database_kind_registry()
    vec_registry = default_vector_store_registry()

    inst = sub.add_parser("install", help="Provision the data-platform on K8s.")
    inst.add_argument("--namespace", default="axiom-data")
    inst.add_argument("--release", default="axiom-data-platform")
    inst.add_argument("--kube-context", help="kubectl context (default: current)")
    inst.add_argument("--axiom-version", default="",
                      help="axiom-os-lm pin (default: chart's appVersion)")
    inst.add_argument("--db-kind", default="postgres",
                      choices=db_registry.kinds(),
                      help="OLTP database kind (provider). Default: postgres.")
    inst.add_argument("--db-mode", default="internal",
                      choices=["internal", "external"],
                      help="`internal` deploys the bundled DB; `external` uses --db-dsn.")
    inst.add_argument("--db-dsn", default="",
                      help="connect string when --db-mode=external")
    inst.add_argument("--vector-kind", default="pgvector",
                      choices=vec_registry.kinds(),
                      help="vector-store kind (provider). Default: pgvector "
                           "(co-locates with --db-kind=postgres).")
    inst.add_argument("--vector-dsn", default="",
                      help="vector-store connect string when not co-located")
    # Each DB + VectorStore provider attaches its own kind-specific flags.
    # We attach ALL providers' flags so help discovers everything; the
    # active provider is selected at runtime from --db-kind / --vector-kind.
    for k in db_registry.kinds():
        db_registry.get(k).add_install_args(inst)
    for k in vec_registry.kinds():
        vec_registry.get(k).add_install_args(inst)

    inst.add_argument("--expose", default="ClusterIP",
                      choices=["ClusterIP", "NodePort", "LoadBalancer"])
    inst.add_argument("--node-port", type=int, default=0)
    inst.add_argument("--bronze-size", default="100Gi")
    inst.add_argument("--rules",
                      help="path to provenance rules TOML (optional; default = quarantine all)")
    inst.add_argument("--dry-run", action="store_true")
    inst.add_argument("--skip-diagnose", action="store_true",
                      help="don't auto-invoke data.diagnose post-install")

    # ---- diagnose --------------------------------------------------------
    diag = sub.add_parser("diagnose", help="Post-install health checks.")
    diag.add_argument("--namespace", default="axiom-data")
    diag.add_argument("--release", default="axiom-data-platform")
    diag.add_argument("--kube-context")

    # ---- ingest ----------------------------------------------------------
    ing = sub.add_parser("ingest", help="Gated source → bronze → RAG pass.")
    ing.add_argument("--connector", required=True)
    ing.add_argument("--since")
    ing.add_argument("--volume-mode", default="confirm",
                     choices=["off", "refuse", "confirm"])

    # ---- register <name> <kind> [kind-specific flags] -------------------
    reg = sub.add_parser(
        "register",
        help="Register a connector — `register <name> <kind> [flags]`.",
    )
    reg.add_argument("name", help="connector name")
    reg.add_argument("--bronze-root", required=True)
    reg.add_argument("--rag-dsn-env", default="DP1_RAG_DSN")
    reg.add_argument("--provenance-rules-file")
    reg.add_argument("--default-disposition", default="quarantine",
                     choices=["allow", "quarantine", "exclude"])
    reg.add_argument("--default-tier", default="rag-community")
    reg.add_argument("--force", action="store_true")
    kind_sub = reg.add_subparsers(dest="kind", required=True,
                                  metavar="kind",
                                  help="source kind — each registers its own flags")
    source_registry = default_source_kind_registry()
    for kind_name in source_registry.kinds():
        provider = source_registry.get(kind_name)
        kp = kind_sub.add_parser(kind_name, help=provider.description)
        provider.add_register_args(kp)

    # ---- unregister ------------------------------------------------------
    unreg = sub.add_parser("unregister", help="Remove a connector.")
    unreg.add_argument("name", help="connector name")

    # ---- list ------------------------------------------------------------
    lst = sub.add_parser("list", help="List registered resources.")
    lst.add_argument("resource", nargs="?", default="connectors",
                     choices=["connectors", "kinds", "db-kinds", "vector-kinds"],
                     help="resource to list (default: connectors)")

    # ---- preflight -------------------------------------------------------
    pre = sub.add_parser(
        "preflight",
        help="Live-verify a connector's auth + access; print fixes for anything wrong.",
    )
    pre.add_argument("name", help="connector name")

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


def _args_to_params(args: argparse.Namespace) -> dict[str, Any]:
    """Translate parsed args → skill params dict."""
    params: dict[str, Any] = {}
    for k, v in vars(args).items():
        if k in ("verb", "json", "kind"):
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
        print(json.dumps({
            "ok": result.ok,
            "value": result.value,
            "errors": result.errors,
            "actions_taken": result.actions_taken,
        }, indent=2, default=str))
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
            print("\n  All good — ingestion will run on the next sensor tick."
                  if v["ok"] else "\n  Fix the items above, then re-run preflight.")
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
    if params.pop("_kind_params_error", None):
        return _emit(
            SkillResult(ok=False, errors=[params["_kind_params_error"]]),
            args.json,
        )
    result = ctx.registry.invoke(f"data.{args.verb}", params, ctx)
    return _emit(result, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
