# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""`axi identity` CLI — thin dispatcher over the identity skills (ADR-056).

Runnable directly: ``python -m axiom.extensions.builtins.identity.cli whoami``.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Optional

from axiom.extensions.builtins.identity.skills import init, status, whoami

_SKILLS = {"whoami": whoami, "status": status, "init": init}


def _context():
    from axiom.infra.principal import resolve_principal
    from axiom.infra.skills import SkillContext

    return SkillContext(
        registry=None,
        state_dir=Path("."),
        logger=logging.getLogger("identity"),
        principal=resolve_principal(),
    )


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="axi identity", description="Local principal identity.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_text in (
        ("whoami", "show the acting principal"),
        ("status", "principal + posture + node floor"),
        ("init", "create/load the local principal keypair"),
    ):
        sub.add_parser(name, help=help_text)

    args = ap.parse_args(argv)
    result = _SKILLS[args.cmd]({}, _context())
    print(json.dumps(result.value, indent=2, sort_keys=True))
    for err in result.errors:
        print(f"error: {err}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
