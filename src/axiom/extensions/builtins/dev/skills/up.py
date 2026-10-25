# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``dev.up`` — run this checkout's node and print the one URL to open.

Re-running it while the node is up changes nothing and prints the same URL,
so it is safe to type whenever you are not sure.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import node, signin
from . import open_vault, resolve_lane


def _email(params: dict[str, Any], root: Path) -> str:
    if params.get("email"):
        return str(params["email"]).strip()
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "config", "user.email"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _source_note(root: Path) -> str:
    """Say so when the `axi` that was typed is not this checkout's code.

    The node runs this checkout's source either way; this is about the
    commands you type next, which run whatever the shared venv was installed
    from.
    """
    import axiom

    here = Path(axiom.__file__).resolve()
    if root.resolve() in here.parents:
        return ""
    return (
        f"note: the `axi` you typed runs {here.parents[1]}; the node runs this "
        f"checkout. Other commands you type will run the other one."
    )


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    lane, errors = resolve_lane(params)
    if errors:
        return SkillResult(ok=False, errors=errors)
    root = Path(lane["root"])
    plan_home = node.home_for(ctx.state_dir, lane["name"])

    record = node.read_record(plan_home)
    if record and node.alive(int(record["pid"])) and not params.get("restart"):
        if node.answers(record["url"]):
            return SkillResult(
                value={**record, "lane": lane["name"], "already_running": True},
                actions_taken=[f"already running: {record['url']}"],
            )
        return SkillResult(
            ok=False,
            errors=[
                f"a node for this checkout is running (pid {record['pid']}) but not "
                f"answering {record['url']}; `dev down`, then `dev up` again",
            ],
        )
    if record and params.get("restart"):
        node.stop(plan_home)

    vault = open_vault(ctx)
    notes: list[str] = []
    sign_in_env: dict[str, str] = {}
    sign_in = None
    choice = str(params.get("sign_in") or "").strip()
    if choice != "none":
        entries = vault.list()
        if choice:
            sign_in = signin.choose(entries, choice)
            if sign_in is None:
                return SkillResult(
                    ok=False,
                    errors=[
                        f"vault entry {choice!r} is not a sign-in client: it needs "
                        "an https issuer_url and a client_id"
                    ],
                )
        else:
            sign_in, said = signin.detect(entries)
            notes += said
        if sign_in is not None:
            sign_in_env = sign_in.environment(label=sign_in.label)

    plan = node.build_plan(
        lane=lane["name"],
        root=root,
        port=int(lane["front"]),
        state_dir=ctx.state_dir,
        extra_roots=[Path(p).resolve() for p in params.get("with") or []],
        sign_in_env=sign_in_env,
    )
    email = _email(params, root)
    if not email:
        return SkillResult(
            ok=False,
            errors=["no email to make the first account with: pass --email, or set git user.email"],
        )
    owner = node.ensure_owner(plan, email, vault)

    pid = node.start(plan)
    ok, why = node.wait_ready(plan, pid, timeout=float(params.get("timeout") or 90))
    if not ok:
        node.stop(plan.home)
        return SkillResult(
            ok=False,
            errors=[why, f"last lines of {plan.log}:", node.tail(plan.log)],
        )
    url = node.served_url(plan.url)
    node.remember_url(plan.home, url)
    if note := _source_note(root):
        notes.append(note)
    return SkillResult(
        value={
            "lane": plan.lane,
            "url": url,
            "pid": pid,
            "port": plan.port,
            "log": str(plan.log),
            "email": owner["email"],
            "account_created": owner["created"],
            "password_secret": owner["secret"],
            "sign_in": sign_in.label if sign_in else None,
            "sign_in_credential": sign_in.credential if sign_in else None,
            "notes": notes,
        },
        actions_taken=[f"started {url} (pid {pid})"],
    )
