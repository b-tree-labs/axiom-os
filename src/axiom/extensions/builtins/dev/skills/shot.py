# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``dev.shot`` — run the checkout's own screenshot command (``[dev].shots``)."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import config
from . import resolve_lane


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    lane, errors = resolve_lane(params)
    if errors:
        return SkillResult(ok=False, errors=errors)
    root = Path(lane["root"])
    try:
        cfg = config.load(root)
    except config.DevConfigError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    if not cfg.shots:
        return SkillResult(
            ok=False, errors=["this checkout declares no [dev] shots command in dev.toml"]
        )
    out = root / ".dev" / "shots"
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    front = f"http://127.0.0.1:{lane['front']}"
    env.update(
        {
            "DEV_URL": front,
            "DEV_NODE_URL": f"http://127.0.0.1:{lane['api']}",
            "DEV_SHOTS_DIR": str(out),
        }
    )
    cmd = config.fill(cfg.shots, port=int(lane["front"]), node_url=env["DEV_NODE_URL"])
    # The checkout's own declared command, split as the web command is (web.py): no shell.
    # nosemgrep: dangerous-subprocess-use-tainted-env-args -- argv is this checkout's own dev.toml command, split; shell is off
    proc = subprocess.run(shlex.split(cmd), cwd=root, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        return SkillResult(
            ok=False,
            errors=[
                "the shots command failed:",
                "\n".join((proc.stderr or proc.stdout).splitlines()[-8:]),
            ],
        )
    files = sorted(p.name for p in out.iterdir())
    return SkillResult(
        value={"dir": str(out), "files": files},
        actions_taken=[f"wrote {len(files)} file(s) to {out}"],
    )
