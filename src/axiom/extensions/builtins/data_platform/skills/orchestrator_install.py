# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data.orchestrator_install``: run the data orchestrator (PULSE's tick loop) as a user service.

See :mod:`..services.orchestrator_unit`. Linux with systemd only; elsewhere the
unit is written for reference and the orchestrator is run under the host's own
supervisor (``python -m axiom.extensions.builtins.data_platform.orchestration``).
"""

from __future__ import annotations

import platform
import shutil
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    from ..services import orchestrator_unit as ou

    env_file = params.get("env_file") or None
    if env_file:
        path = Path(str(env_file)).expanduser().resolve()
        if not path.is_file():
            return SkillResult(ok=False, errors=[
                f"{path} does not exist: pass the env file your node's services already read "
                f"(the one that sets AXIOM_DB_URL); this verb never writes a credential itself"])
        env_file = str(path)
    has_systemd = platform.system() == "Linux" and shutil.which("systemctl") is not None
    write_only = bool(params.get("write_only")) or not has_systemd
    value = ou.install(env_file=env_file, write_only=write_only, force=bool(params.get("force")))
    if value.get("refused"):
        return SkillResult(ok=False, value=value, errors=[value["refused"]])
    if value.get("error"):
        return SkillResult(ok=False, value=value, errors=[value["error"]])
    notes = [f"wrote {value['unit']}"]
    if value["started"]:
        notes.append(f"enabled and started {ou.UNIT}")
    elif not has_systemd:
        notes.append("no systemd here: run the orchestrator under this host's supervisor instead")
    if not env_file:
        notes.append("no --env-file: the orchestrator uses the default database URL; pass the env file "
                     "your node's services read if they set AXIOM_DB_URL")
    text = "\n".join([*notes, "next:", *(f"  {s}" for s in value["next"])])
    return SkillResult(ok=True, value={**value, "text": text}, actions_taken=notes)
