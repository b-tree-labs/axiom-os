# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.wire_git`` — point git's credential lookup at a stored credential.

Two effects:

1. Records ``git_host`` (+ optional ``git_username``) on the credential's
   metadata so ``secrets.git_credential get`` matches the host.
2. Writes a ``credential.https://<host>.helper`` entry via ``git config``
   (``--global`` by default; ``--config-file`` targets an explicit file —
   tests use this so no user config is ever touched).
"""

from __future__ import annotations

import subprocess
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..foreign.store import ForeignCredentialStore

DEFAULT_HELPER = "!axi secrets git-credential"


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = params.get("name")
    host = params.get("host")
    if not name or not host:
        return SkillResult(ok=False, errors=[
            "wire-git needs a credential name and --host",
        ])
    store = params.get("_store") or ForeignCredentialStore(ctx.state_dir)

    try:
        store.update_metadata(
            name,
            git_host=host,
            git_username=params.get("username"),
        )
    except (KeyError, ValueError) as exc:
        return SkillResult(ok=False, errors=[f"wire-git failed: {exc}"])

    helper = params.get("helper_command") or DEFAULT_HELPER
    if params.get("config_file"):
        target = ["--file", str(params["config_file"])]
        target_desc = str(params["config_file"])
    else:
        target = ["--global"]
        target_desc = "global git config"

    section = f"credential.https://{host}"
    commands = [
        # Clear our helper slot first so re-wiring stays idempotent.
        ["git", "config", *target, "--unset-all", f"{section}.helper"],
        ["git", "config", *target, "--add", f"{section}.helper", helper],
    ]
    for i, cmd in enumerate(commands):
        cp = subprocess.run(cmd, capture_output=True, text=True, check=False)
        # --unset-all returns 5 when nothing was set; only the add must pass.
        if i == len(commands) - 1 and cp.returncode != 0:
            return SkillResult(ok=False, errors=[
                f"git config failed (rc={cp.returncode}): {cp.stderr.strip()}",
            ])

    return SkillResult(
        ok=True,
        value={"name": name, "host": host, "helper": helper,
               "config_target": target_desc},
        actions_taken=[
            f"mapped {name!r} to host {host}",
            f"configured credential helper for https://{host} in {target_desc}",
        ],
    )


__all__ = ["run", "DEFAULT_HELPER"]
