# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`axi observe` — thin argparse → skill-fn dispatcher per ADR-056."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from axiom.infra.skills import SkillContext

from .skills import bind_default


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="axi observe",
                                description="Observability substrate (Langfuse)")
    sub = p.add_subparsers(dest="verb", required=True)

    install = sub.add_parser("install", help="Provision Langfuse on K8s")
    install.add_argument("--namespace", default="axiom-observability")
    install.add_argument("--release", default="axiom-observability")
    install.add_argument("--kube-context")
    install.add_argument("--expose", default="ClusterIP")
    install.add_argument("--node-port", type=int, default=0)
    install.add_argument("--dry-run", action="store_true")
    install.add_argument("--skip-diagnose", action="store_true")

    verify = sub.add_parser("verify", help="Probes (preflight | postinstall)")
    verify.add_argument("--phase", choices=("preflight", "postinstall"),
                        default="preflight")
    verify.add_argument("--namespace", default="axiom-observability")
    verify.add_argument("--host")
    verify.add_argument("--public-key")
    verify.add_argument("--secret-key")

    diag = sub.add_parser("diagnose", help="Post-install health")
    diag.add_argument("--namespace", default="axiom-observability")
    diag.add_argument("--release", default="axiom-observability")
    diag.add_argument("--kube-context")

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    registry = bind_default()
    ctx = SkillContext(
        registry=registry,
        state_dir=Path.home() / ".axi" / "observability",
        logger=logging.getLogger("axi.observe"),
        user_prompt=None,
    )
    params = {k.replace("-", "_"): v for k, v in vars(args).items() if k != "verb"}
    skill_name = f"observe.{args.verb}"
    result = registry.invoke(skill_name, params, ctx)
    print(json.dumps({
        "ok": result.ok,
        "value": result.value,
        "actions": result.actions_taken,
        "errors": result.errors,
    }, indent=2, default=str))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
