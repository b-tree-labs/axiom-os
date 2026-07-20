# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``axi serve`` — launch the one composed HTTP app (spec-serve §7).

Per ADR-056 the CLI verb is a thin wrapper over a skill function: it
translates flags → a params dict and dispatches to ``serve.run`` via the
``SkillRegistry``. No serving logic lives here.

This folds the legacy ``serve`` cmd (the standalone chat HTTP API,
formerly ``serve_cli:main``) into the composed model — the chat API
becomes a mounted ``/chat`` router on the composed app (PRD open
question 2; the mount itself is a follow-up seam).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from axiom.infra.paths import get_user_state_dir
from axiom.infra.skills import SkillContext, SkillResult

from . import skills as serve_skills

_PROG = "axi serve"


def get_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=_PROG,
        description="Launch the composed Axiom HTTP app (serve substrate).",
    )
    p.add_argument("--host", default="127.0.0.1",
                   help="bind host (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8787,
                   help="bind port (default: 8787)")
    p.add_argument("--profile", default=None,
                   help="deployment profile gating which routers mount")
    p.add_argument("--list", action="store_true",
                   help="print the composed route table without binding")
    p.add_argument("--log-level", default="warning",
                   help="uvicorn log level (default: warning)")
    p.add_argument("--insecure", action="store_true",
                   help="serve auth-required mounts WITHOUT authz enforcement "
                        "(dev/loopback only; default is fail-closed)")
    p.add_argument("--json", action="store_true",
                   help="emit the SkillResult as JSON")
    return p


def _build_ctx() -> SkillContext:
    return SkillContext(
        registry=serve_skills.bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.serve"),
    )


def _emit(result: SkillResult, as_json: bool) -> int:
    if as_json:
        print(json.dumps({
            "ok": result.ok,
            "value": result.value,
            "errors": result.errors,
            "actions_taken": result.actions_taken,
        }, indent=2, default=str))
        return result.exit_code
    if result.value and isinstance(result.value, dict) and "routes" in result.value:
        routes = result.value["routes"]
        if not routes:
            print("(no routes mounted)")
        for r in routes:
            authz = "authz" if r["requires_authz"] else "public"
            zone = r.get("trust_zone") or "-"
            print(f"  {r['prefix']:<22} {r['extension']:<16} {authz:<7} {zone}")
    for action in result.actions_taken:
        print(f"• {action}")
    if not result.ok:
        for err in result.errors:
            print(f"ERROR: {err}", file=sys.stderr)
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    args = get_parser().parse_args(argv)
    ctx = _build_ctx()
    params = {
        "host": args.host,
        "port": args.port,
        "profile": args.profile,
        "list": args.list,
        "log_level": args.log_level,
        "insecure": args.insecure,
    }
    result = ctx.registry.invoke("serve.run", params, ctx)
    return _emit(result, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
