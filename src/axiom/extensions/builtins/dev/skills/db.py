# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``dev.db_*`` — this lane's own database: status, migrate, reset, seed.

Every verb aims at ``lane.database`` on the server named by ``--db-url`` /
``$AXIOM_DB_URL`` and nothing else, so a dev run can never reach the shared or
production database. Reset recreates a regenerable development database, which
is why it is a reversible write behind an explicit ``--yes`` rather than a plan.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import config, node, schema
from . import resolve_lane
from .up import _database_url


def _target(params: dict[str, Any], ctx: SkillContext):
    lane, errors = resolve_lane(params)
    if errors:
        return None, errors
    if not lane.get("isolated"):
        return None, [
            f"lane {lane['name']!r} is unmanaged, so it has no database of its own to manage.",
            f"claim one: axi lane claim --replace --name {lane['name']}",
        ]
    root = Path(lane["root"])
    template, errs = _database_url(params, root, ctx)
    if errs:
        return None, errs
    try:
        dsn = schema.lane_dsn(template, lane["database"])
    except schema.SchemaError as exc:
        return None, [str(exc)]
    roots = [*(Path(p).resolve() for p in params.get("with") or []), *node.source_roots(root)]
    return {
        "lane": lane,
        "root": root,
        "dsn": dsn,
        "roots": roots,
        "home": node.home_for(ctx.state_dir, lane["name"]),
        "dsn_var": lane.get("dsn_var") or node.DB_URL_VAR,
    }, []


def status(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    t, errors = _target(params, ctx)
    if t is None:
        return SkillResult(ok=False, errors=errors)
    try:
        if not schema.database_exists(t["dsn"]):
            return SkillResult(
                value={
                    "database": t["lane"]["database"],
                    "exists": False,
                    "extensions": {},
                    "drift": [],
                },
                actions_taken=[
                    f"{t['lane']['database']}: does not exist yet (dev up or dev db migrate creates it)"
                ],
            )
        report = schema.probe(t["root"], t["roots"], t["dsn"], t["dsn_var"])
    except schema.SchemaError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    drifts = schema.classify(report)
    lines = [
        f"{n}: database {v['current'] or 'base'}, checkout {', '.join(v['heads']) or 'base'}"
        for n, v in sorted(report.items())
    ]
    lines += [schema.describe(d) for d in drifts]
    return SkillResult(
        value={
            "database": t["lane"]["database"],
            "exists": True,
            "extensions": {
                n: {"database": v["current"], "checkout": v["heads"]} for n, v in report.items()
            },
            "drift": [d.__dict__ for d in drifts],
            "recorded": schema.read_record(t["home"]),
        },
        actions_taken=lines,
    )


def migrate(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    t, errors = _target(params, ctx)
    if t is None:
        return SkillResult(ok=False, errors=errors)
    try:
        out = schema.sync(t["root"], t["roots"], t["dsn"], t["home"], t["dsn_var"])
    except schema.SchemaError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    said = f"{t['lane']['database']}: at this checkout's heads ({out['commit']})"
    if out["created"]:
        said += ", created"
    return SkillResult(
        value=out, actions_taken=[said, *(f"problem: {p}" for p in out.get("problems", []))]
    )


def reset(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    t, errors = _target(params, ctx)
    if t is None:
        return SkillResult(ok=False, errors=errors)
    name = t["lane"]["database"]
    asked = str(params.get("database") or name)
    if asked != name:
        return SkillResult(
            ok=False,
            errors=[
                f"refusing to reset {asked!r}: only this lane's own database ({name}) can be reset"
            ],
        )
    if not params.get("yes"):
        return SkillResult(
            ok=False,
            errors=[f"this drops and recreates {name}, and everything in it. Pass --yes to do it."],
        )
    try:
        schema.reset_database(t["dsn"], name)
        out = schema.sync(t["root"], t["roots"], t["dsn"], t["home"], t["dsn_var"])
    except schema.SchemaError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(
        value={"database": name, "reset": True, **out},
        actions_taken=[f"{name}: dropped, recreated and migrated for {out['commit']}"],
    )


def seed(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    t, errors = _target(params, ctx)
    if t is None:
        return SkillResult(ok=False, errors=errors)
    try:
        cfg = config.load(t["root"])
    except config.DevConfigError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    if ":" not in cfg.seed:
        return SkillResult(
            ok=False,
            errors=[
                'this checkout declares no seed: add [dev] seed = "package.module:function" to dev.toml'
            ],
        )
    mod, fn = cfg.seed.split(":", 1)
    try:
        schema.sync(t["root"], t["roots"], t["dsn"], t["home"], t["dsn_var"])
    except schema.SchemaError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(str(p) for p in t["roots"])
    env["AXIOM_DB_URL"] = t["dsn"]
    env[t["dsn_var"]] = t["dsn"]
    if cfg.config_dir:
        env["AXIOM_CONFIG_DIR"] = str((t["root"] / cfg.config_dir).resolve())
    code = f"import importlib; importlib.import_module({mod!r}).{fn}()"
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=t["root"], env=env, capture_output=True, text=True
    )
    if proc.returncode != 0:
        return SkillResult(
            ok=False,
            errors=[
                f"seed {cfg.seed} failed:",
                "\n".join((proc.stderr or proc.stdout).splitlines()[-8:]),
            ],
        )
    return SkillResult(
        value={"seed": cfg.seed, "database": t["lane"]["database"]},
        actions_taken=[f"seeded {t['lane']['database']} with {cfg.seed} (sample data)"],
    )
