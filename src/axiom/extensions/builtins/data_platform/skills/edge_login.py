# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.edge_login_<verb>``: named Box logins for edge nodes, held until install.

``axi data edge-login reserve|capture|renew|attach|list``. The login itself is
made by the storage login command (``data box-login``); these verbs hold it in this host's vault under a name,
say when it needs renewing, and move it onto the node. No result carries a login.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..forward.edge_logins import (
    EdgeLoginError,
    list_logins,
)
from ..forward.edge_logins import (
    attach as _attach,
)
from ..forward.edge_logins import (
    capture as _capture,
)
from ..forward.edge_logins import (
    renew as _renew,
)
from ..forward.edge_logins import (
    reserve as _reserve,
)


def _name(params: dict[str, Any]) -> str:
    return str(params.get("name") or "")


def _read_login(params: dict[str, Any]) -> tuple[str | None, str | None]:
    raw = str(params.get("login_file") or "").strip()
    if not raw:
        return None, "give --login-file, the file `axi data box-login --out` wrote"
    path = Path(raw).expanduser()
    if not path.is_file():
        return None, f"no Box login file at {path}"
    try:
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError):
        return None, f"cannot read {path} as a Box login"


def _delete_hint(params: dict[str, Any]) -> str:
    return f"delete {params.get('login_file')}: the vault now holds the only copy this host keeps"


def reserve(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    try:
        row = _reserve(Path(ctx.state_dir), _name(params), str(params.get("folder_id") or ""),
                       str(params.get("app") or ""))
    except EdgeLoginError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(
        value=row,
        actions_taken=[
            f"reserved {row['name']} (folder {row['folder_id']}, app {row['app']})",
            f"next: axi data box-login --app {row['app']} --out box-edge-{row['name']}.json, "
            f"then axi data edge-login capture {row['name']} --login-file box-edge-{row['name']}.json",
        ],
    )


def capture(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    line, err = _read_login(params)
    if err:
        return SkillResult(ok=False, errors=[err])
    try:
        row = _capture(Path(ctx.state_dir), _name(params), line or "")
    except EdgeLoginError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(value=row, actions_taken=[f"held a Box login for {row['name']} as {row['credential']}",
                                                 _delete_hint(params)])


def renew(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    line, err = _read_login(params)
    if err:
        return SkillResult(ok=False, errors=[err])
    try:
        row = _renew(Path(ctx.state_dir), _name(params), line or "")
    except EdgeLoginError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(value=row, actions_taken=[f"renewed the Box login for {row['name']}", _delete_hint(params)])


def attach(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    dest = str(params.get("dest_state_dir") or "").strip()
    if not dest:
        return SkillResult(ok=False, errors=["attach needs dest_state_dir, the node's state dir"])
    try:
        row = _attach(Path(ctx.state_dir), _name(params), str(params.get("node") or ""), Path(dest).expanduser())
    except EdgeLoginError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(
        value=row,
        actions_taken=[
            f"moved {row['name']} to {row['node']}; this host no longer holds it",
            f"on {row['node']}: axi data forward --box-remote <folder> --box-login {row['credential']} "
            f"--box-app {row['app']}",
        ],
    )


def list_all(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    try:
        rows = list_logins(Path(ctx.state_dir))
    except EdgeLoginError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    due = [r["name"] for r in rows if r["needs_renewal"]]
    actions = [f"needs renewal: {', '.join(due)} (each row's `renew` lists the steps)"] if due else []
    return SkillResult(value={"logins": rows}, actions_taken=actions)


__all__ = ["attach", "capture", "list_all", "renew", "reserve"]
