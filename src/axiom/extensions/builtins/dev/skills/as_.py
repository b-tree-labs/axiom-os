# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``dev.as`` — how to sign in as a tenant's persona, on that tenant's own host."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import config, node, tenants
from . import resolve_lane


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    lane, errors = resolve_lane(params)
    if errors:
        return SkillResult(ok=False, errors=errors)
    try:
        cfg = config.load(Path(lane["root"]))
    except config.DevConfigError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    if not cfg.tenants:
        return SkillResult(ok=False, errors=["this checkout declares no [[tenant]] in dev.toml"])
    target = str(params.get("target") or cfg.tenants[0].id)
    tid, _, persona = target.partition(":")
    tenant = next((t for t in cfg.tenants if t.id == tid), None)
    if tenant is None:
        return SkillResult(
            ok=False,
            errors=[f"no tenant {tid!r}; declared: {', '.join(t.id for t in cfg.tenants)}"],
        )
    try:
        roles = tenants.personas_for(cfg)
    except tenants.TenantProblem as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    persona = config.canonical_persona(persona) if persona else (
        "site-admin" if "site-admin" in roles else sorted(roles)[0])
    if persona not in roles:
        return SkillResult(
            ok=False, errors=[f"no persona {persona!r}; declared: {', '.join(sorted(roles))}"]
        )
    home = node.home_for(ctx.state_dir, lane["name"])
    if not (node.read_record(home) or {}).get("pid"):
        return SkillResult(ok=False, errors=["the node is not up: run `axi dev up` first"])
    base = tenants.host_for(tenant.id, int(lane["front"]) if cfg.web else int(lane["api"]))
    secret = tenants.secret_for(lane["name"], tenant.id, persona)
    out = {
        "tenant": tenant.id,
        "site": tenant.site,
        "persona": persona,
        "role": roles[persona],
        "url": base + "/",
        "email": tenants.email_for(tenant.id, persona),
        "password_secret": secret,
    }
    if params.get("open"):
        subprocess.run(["open", out["url"]], check=False)
    return SkillResult(
        value=out,
        actions_taken=[
            f"{out['url']}",
            f"sign in as {out['email']} (role {out['role']}, site {tenant.site})",
            f"password: axi vault get {secret} --reveal | pbcopy",
        ],
    )
