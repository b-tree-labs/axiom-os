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


def _database_url(params: dict[str, Any], root: Path) -> tuple[str, list[str]]:
    """The database the node will use, or why there is none.

    Never the platform default. With ``AXIOM_DB_URL`` unset a node falls back
    to ``localhost:5432``, which on a machine running another local stack is
    somebody else's database; a terminal that had not loaded the checkout's
    env file reported "up" against exactly that (#1788).
    """
    import os

    url = str(params.get("db_url") or os.environ.get(node.DB_URL_VAR) or "").strip()
    if url:
        return url, []
    errors = [
        f"{node.DB_URL_VAR} is not set, so the node would use the platform default "
        "(localhost:5432), which may be somebody else's database. Refusing to guess.",
    ]
    env_file = node.env_file_sets_db_url(root)
    if env_file is not None:
        errors.append(
            f"this checkout's {env_file.name} sets it: activate the venv and load it "
            f"(`direnv allow`, or `source {env_file}`), then run dev up again"
        )
    else:
        errors.append(
            "activate the venv and load this checkout's env file, which sets it, "
            "then run dev up again"
        )
    errors.append("or name the database: dev up --db-url postgresql://USER@HOST:PORT/DBNAME")
    return "", errors


def _vault_refusal(exc: Exception) -> list[str]:
    from axiom.extensions.builtins.secrets.vault_settings import ENV_BACKEND

    return [
        f"could not store the first account's password in the vault: {exc}",
        "no account was made. For development, keep it in a file only you can read "
        f"instead of the keychain: export {ENV_BACKEND}=file, then run dev up again",
    ]


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    lane, errors = resolve_lane(params)
    if errors:
        return SkillResult(ok=False, errors=errors)
    root = Path(lane["root"])
    plan_home = node.home_for(ctx.state_dir, lane["name"])

    record = node.read_record(plan_home)
    if record and node.alive(int(record["pid"])) and not params.get("restart"):
        if node.answers(record["url"]):
            # Say how to sign in here too: a second `dev up` is how people find the address again.
            return SkillResult(
                value={
                    **record,
                    "lane": lane["name"],
                    "already_running": True,
                    "email": _email(params, root),
                    "password_secret": node.secret_name(lane["name"]),
                },
                actions_taken=[f"already running: {record['url']}"],
            )
        return SkillResult(
            ok=False,
            errors=[
                f"a node for this checkout is running (pid {record['pid']}) but not "
                f"answering {record['url']}; `dev down`, then `dev up` again",
            ],
        )
    db_url, db_errors = _database_url(params, root)
    if db_errors:
        return SkillResult(ok=False, errors=db_errors)
    database = node.describe_database(db_url)

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
        db_url=db_url,
    )
    email = _email(params, root)
    if not email:
        return SkillResult(
            ok=False,
            errors=["no email to make the first account with: pass --email, or set git user.email"],
        )
    try:
        owner = node.ensure_owner(plan, email, vault)
    except RuntimeError as exc:  # a keychain write, or a vault backend that is not there
        return SkillResult(ok=False, errors=_vault_refusal(exc))

    pid = node.start(plan, database=database)
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
            "database": database,
            "email": owner["email"],
            "account_created": owner["created"],
            "password_secret": owner["secret"],
            "sign_in": sign_in.label if sign_in else None,
            "sign_in_credential": sign_in.credential if sign_in else None,
            "notes": notes,
        },
        actions_taken=[f"started {url} (pid {pid})"],
    )
