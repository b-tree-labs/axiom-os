# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``dev.up`` — run this checkout's node and print the one URL to open.

Re-running it while the node is up changes nothing and prints the same URL,
so it is safe to type whenever you are not sure.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import config, node, running, schema, server, signin, tenants
from .. import web as webmod
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


def _database_url(
    params: dict[str, Any], root: Path, ctx: SkillContext | None = None
) -> tuple[str, list[str]]:
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
    if ctx is not None:
        from axiom.extensions.builtins.lane.registry import Registry
        from axiom.extensions.builtins.lane.skills import lanes_path

        found, fixes = server.acquire(
            open_vault(ctx), Registry(params.get("registry") or lanes_path())
        )
        if found:
            return found, []
        errors = [
            f"{node.DB_URL_VAR} is not set and no database server was found. The node will not "
            "fall back to the platform default (localhost:5432), which may be somebody else's database.",
            *fixes,
        ]
        env_file = node.env_file_sets_db_url(root)
        if env_file is not None:
            errors.append(
                f"this checkout's {env_file.name} sets {node.DB_URL_VAR}: load it (`direnv allow`, or `source {env_file}`)"
            )
        return "", errors
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


def _from_bound_site(vault: Any) -> tuple[signin.SignInClient | None, list[str]]:
    """The bound site's public client, recorded in the vault, or what to say instead."""
    offer, said = signin.from_bound_site()
    if offer is None:
        return None, said
    try:
        return signin.record_offer(vault, offer), []
    except (RuntimeError, ValueError, OSError) as exc:
        return None, [
            f"could not record the site's sign-in client in the vault ({exc}); "
            "using password sign-in"
        ]


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    # A topology, one node of one, or a node of another kind than the
    # checkout's own: each node on its own lane (see ..graph).
    if params.get("topology") or params.get("node") or params.get("kind") == "edge":
        from .. import graph

        return graph.up(params, ctx)
    lane, errors = resolve_lane(params)
    if errors:
        return SkillResult(ok=False, errors=errors)
    out = serve_lane(lane, params, ctx)
    if out.ok and params.get("kind"):
        out.value["kind"] = params["kind"]
    return out


def serve_lane(
    lane: dict[str, Any],
    params: dict[str, Any],
    ctx: SkillContext,
    *,
    extra_env: dict[str, str] | None = None,
    web: bool = True,
    hosted: tuple[config.Tenant, ...] | None = None,
) -> SkillResult:
    """Run a serving node on ``lane``: its database, first account, node on api, web on front.

    ``extra_env`` is a topology node's own wiring (its state, keys file, served
    sites). ``web=False`` leaves the web server to another node. ``hosted``
    replaces dev.toml's tenants with the ones this node hosts.
    """
    if not lane.get("isolated"):
        return SkillResult(
            ok=False,
            errors=[
                f"lane {lane['name']!r} is unmanaged: it has no database of its own, and a dev "
                "node never shares one.",
                f"claim one: axi lane claim --replace --name {lane['name']}",
            ],
        )
    root = Path(lane["root"])
    try:
        cfg = config.load(root)
    except config.DevConfigError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    plan_home = node.home_for(ctx.state_dir, lane["name"])

    record = node.read_record(plan_home)
    if record and node.alive(int(record["pid"])) and not params.get("restart"):
        if node.answers(record["url"]):
            # Say how to sign in here too: a second `dev up` is how people find the address again.
            # What is running is the node's, never this request's (#1791).
            now = running.of_record(record, plan_home, root, fallback_email=_email(params, root))
            web_rec = webmod.read_record(plan_home)
            web_up = bool(web_rec and node.alive(int(web_rec["pid"])))
            front = (
                webmod.url_for(lane["front"], cfg.web.ready_path) if web_up and cfg.web else None
            )
            return SkillResult(
                value={
                    **record,
                    "lane": lane["name"],
                    "front": lane["front"],
                    "api": lane["api"],
                    "node_url": record["url"],
                    "url": front or record["url"],
                    "web": {"up": web_up, "url": front},
                    "already_running": True,
                    "email": (now["sign_in"] or {}).get("email") or "",
                    "password_secret": node.secret_name(lane["name"]),
                    "running": now,
                    "switch": running.mismatches(
                        now,
                        email=str(params.get("email") or "").strip(),
                        site=(os.environ.get(running.SITE_VAR) or "").strip(),
                    ),
                },
                actions_taken=[f"already running: {front or record['url']}"],
            )
        return SkillResult(
            ok=False,
            errors=[
                f"a node for this checkout is running (pid {record['pid']}) but not "
                f"answering {record['url']}; `dev down`, then `dev up` again",
            ],
        )
    template, db_errors = _database_url(params, root, ctx)
    if db_errors:
        return SkillResult(ok=False, errors=db_errors)
    try:
        db_url = schema.lane_dsn(template, lane["database"])
    except schema.SchemaError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    database = node.describe_database(db_url)

    if record and params.get("restart"):
        node.stop(plan_home)

    vault = open_vault(ctx)
    notes: list[str] = []
    sign_in_env: dict[str, str] = {}
    sign_in = None
    source = ""
    choice = str(params.get("sign_in") or "").strip()
    hand = params.get("sign_in_client") or []
    if hand and choice != "none":
        # #1717 item 3: the site's public client typed by hand, for a site node
        # that does not publish one yet. Recorded, then offered like the bound path.
        if len(hand) != 2:
            return SkillResult(
                ok=False, errors=["--sign-in-client takes two values: <issuer_url> <client_id>"]
            )
        offer, why = signin.offer_from_args(hand[0], hand[1])
        if offer is None:
            return SkillResult(ok=False, errors=[why])
        try:
            sign_in = signin.record_offer(vault, offer, notes=signin.HAND_ENTERED_NOTE)
        except (RuntimeError, ValueError, OSError) as exc:
            return SkillResult(
                ok=False, errors=[f"could not record the sign-in client in the vault: {exc}"]
            )
        source = "entered by hand"
        sign_in_env = sign_in.environment(label=sign_in.label)
    elif choice != "none":
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
            if not said and (sign_in is None or sign_in.public):
                # A bound install signs in with its site's SSO by default
                # (#1717): ask the site every time, so a changed or withdrawn
                # client is followed. A confidential client in the vault (the
                # owner's) is never displaced, and `--sign-in none` asks nobody.
                offered, said = _from_bound_site(vault)
                if offered is not None:
                    sign_in, source = offered, "bound site"
                elif (
                    signin.NOT_ENABLED in said
                    and sign_in is not None
                    and (sign_in.credential == signin.entry_name(signin.bound_site_name()))
                ):
                    sign_in = None  # the site withdrew the client we recorded from it
                elif sign_in is not None:
                    # The site could not be asked: the client recorded from it earlier stands.
                    said = [
                        n.replace("using password sign-in", f"using {sign_in.credential}")
                        for n in said
                    ]
                notes += said
        if sign_in is not None:
            sign_in_env = sign_in.environment(label=sign_in.label)

    plan = node.build_plan(
        lane=lane["name"],
        root=root,
        port=int(lane["api"]),
        state_dir=ctx.state_dir,
        extra_roots=[Path(p).resolve() for p in params.get("with") or []],
        sign_in_env=sign_in_env,
        db_url=db_url,
    )
    if cfg.config_dir:
        # This run only: the repo's own config, never copied into ~/.axi.
        plan.env["AXIOM_CONFIG_DIR"] = str((root / cfg.config_dir).resolve())
    if lane.get("dsn_var") and lane["dsn_var"] != node.DB_URL_VAR:
        plan.env[lane["dsn_var"]] = db_url
    # Every dev node reads issued keys from its own file (hot-reloaded), so an
    # edge enrolled later (`dev up --kind edge --home ...`) is accepted without
    # a restart. Missing means no keys, which refuses every bearer.
    plan.env["AXIOM_GATE_API_KEYS_FILE"] = str(plan.home / "api-keys.json")
    plan.env.update(extra_env or {})
    if hosted is not None:
        from dataclasses import replace

        cfg = replace(cfg, tenants=hosted)
    if not web:
        from dataclasses import replace

        cfg = replace(cfg, web=None)
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

    try:
        migrated = schema.sync(
            root,
            [*(Path(p).resolve() for p in params.get("with") or []), *node.source_roots(root)],
            db_url,
            plan.home,
            lane.get("dsn_var") or node.DB_URL_VAR,
        )
    except schema.SchemaError as exc:
        return SkillResult(ok=False, errors=[f"{database}: {exc}"])

    people: list[dict[str, str]] = []
    if cfg.tenants:
        try:
            for t in cfg.tenants:
                tenants.resolve_config(root, t)
            people = tenants.ensure_accounts(
                accounts=plan.accounts, lane=lane["name"], cfg=cfg, vault=vault
            )
        except (tenants.TenantProblem, RuntimeError) as exc:
            return SkillResult(ok=False, errors=[str(exc)])
        # The node serves exactly these sites, as a partner's would (axiom.infra.site_scope).
        plan.env[tenants.SERVED_SITES_ENV] = tenants.served_sites(cfg)

    notes += [
        f"migration problem (reported by the platform's runner): {p}"
        for p in migrated.get("problems", [])
    ]

    sso_reachable = signin.answers(sign_in) if sign_in else True
    if not sso_reachable:
        notes.append(
            f"{sign_in.issuer_host} cannot be reached: the node still requires a sign-in, "
            "SSO is refused until the provider answers, and a password works meanwhile"
        )
    now = running.collect(
        checkout=root,
        roots=[*(Path(p).resolve() for p in params.get("with") or []), *node.source_roots(root)],
        database=database,
        email=owner["email"],
        sso_host=sign_in.issuer_host if sign_in else None,
        sso_source=source or None,
        sso_reachable=sso_reachable,
        env=plan.env,
    )
    pid = node.start(plan, database=database, email=owner["email"], running=now)
    ok, why = node.wait_ready(plan, pid, timeout=float(params.get("timeout") or 90))
    if not ok:
        node.stop(plan.home)
        return SkillResult(
            ok=False,
            errors=[why, f"last lines of {plan.log}:", node.tail(plan.log)],
        )
    node_url = node.served_url(plan.url)
    node.remember_url(plan.home, node_url)
    front_url = None
    web_pid = None
    if cfg.web is not None:
        web_pid = webmod.start(
            cfg.web,
            root=root,
            home=plan.home,
            front=int(lane["front"]),
            node_url=plan.url.rstrip("/"),
            base_env=dict(plan.env),
        )
        front_url = webmod.url_for(int(lane["front"]), cfg.web.ready_path)
        ok, why = webmod.wait_ready(front_url, web_pid, timeout=float(params.get("timeout") or 90))
        if not ok:
            webmod.stop(plan.home)
            node.stop(plan.home)
            return SkillResult(
                ok=False,
                errors=[
                    why,
                    f"last lines of {plan.home / 'web.log'}:",
                    node.tail(plan.home / "web.log"),
                ],
            )
    url = front_url or node_url
    if note := _source_note(root):
        notes.append(note)
    return SkillResult(
        value={
            "lane": plan.lane,
            "url": url,
            "front": lane["front"],
            "api": lane["api"],
            "node_url": node_url,
            "node_port": lane["api"],
            "web_port": lane["front"] if web_pid is not None else None,
            "pid": pid,
            "port": plan.port,
            "log": str(plan.log),
            "database": database,
            "schema": {"created": migrated["created"], "commit": migrated["commit"]},
            "web": {"up": web_pid is not None, "url": front_url, "pid": web_pid},
            "tenants": [
                {
                    "id": t.id,
                    "site": t.site,
                    "url": tenants.host_for(t.id, int(lane["front"] if cfg.web else lane["api"])),
                }
                for t in cfg.tenants
            ],
            "personas": people,
            "email": owner["email"],
            "account_created": owner["created"],
            "password_secret": owner["secret"],
            "sign_in": sign_in.label if sign_in else None,
            "sign_in_credential": sign_in.credential if sign_in else None,
            "sign_in_public": bool(sign_in and sign_in.public),
            "sign_in_host": sign_in.issuer_host if sign_in else None,
            "sign_in_source": source or None,
            "running": now,
            "notes": notes,
        },
        actions_taken=[f"started {url} (pid {pid})"],
    )
