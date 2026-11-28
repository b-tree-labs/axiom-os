# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``app.variants`` (read) and ``app.run`` (CLI-only), per ADR-056.

``app.variants`` lists the ``[variant.<name>]`` records (``axiom.infra.variants``):
port, path, realm, cookie, tenants and flag layer for each.

``app.run`` starts one variant, or all three, as supervised copies of the one
served app (``axiom.infra.switch.Supervisor``, the same supervisor ``serve
--supervise`` uses): each on its own socket, with its variant's environment, so
SIGHUP switches each with no refused request. ``dry_run`` returns the plan and
starts nothing.
"""

from __future__ import annotations

import os
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult
from axiom.infra.variants import Variant, VariantError, get_variant, load_variants

#: Seams a test replaces; production uses the platform's one supervisor.
_SUPERVISOR: Any = None
_RUN_GROUP: Any = None


def _supervisor_cls():
    if _SUPERVISOR is not None:
        return _SUPERVISOR
    from axiom.infra.switch import Supervisor

    return Supervisor


def _run_group():
    if _RUN_GROUP is not None:
        return _RUN_GROUP
    from axiom.infra.switch import run_group_forever

    return run_group_forever


def _selected(params: dict[str, Any]) -> list[Variant]:
    table = load_variants(params.get("config") or None)
    if params.get("all"):
        return [table[n] for n in sorted(table, key=lambda n: table[n].port)]
    name = str(params.get("variant") or "").strip()
    if not name:
        raise VariantError("name a variant (--variant axiom|product|site) or pass --all")
    return [get_variant(name, table)]


def plan_for(v: Variant, *, host: str = "127.0.0.1", log_level: str = "warning") -> dict[str, Any]:
    from axiom.extensions.builtins.http.skills.serve import child_argv

    return {
        **v.describe(),
        "host": host,
        "url": f"http://{host}:{v.port}/",
        "argv": child_argv(profile=v.profile or None, log_level=log_level),
        "env": v.child_env(),
    }


def _lines(plans: list[dict[str, Any]], verb: str) -> list[str]:
    out = []
    for p in plans:
        out.append(
            f"{verb} {p['name']:<8} {str(p['url']).ljust(24)} path {str(p['path']).ljust(14)} realm {p['realm']} "
            f"(cookie {p['cookie']}) tenants {', '.join(p['tenants']) or 'the install’s sites'}"
        )
    return out


def variants(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """The web app's variants: port, path, session realm, tenants and flag layer for each."""
    try:
        table = load_variants(params.get("config") or None)
    except (OSError, ValueError) as exc:
        return SkillResult(ok=False, errors=[f"variants: {exc}"])
    rows = [table[n].describe() for n in sorted(table, key=lambda n: table[n].port)]
    lines = [
        f"{r['name']:<8} :{r['port']}  {str(r['path']).ljust(14)} realm {str(r['realm']).ljust(14)} "
        f"{'isolated' if r['isolated'] else 'in place'}  {r['composition']}"
        for r in rows
    ]
    return SkillResult(ok=True, value={"variants": rows}, actions_taken=lines)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Start a variant (or ``all``) under the supervisor; ``dry_run`` only plans."""
    host = str(params.get("host") or "127.0.0.1")
    log_level = str(params.get("log_level") or "warning")
    try:
        chosen = _selected(params)
    except (OSError, ValueError) as exc:
        return SkillResult(ok=False, errors=[f"app run: {exc}"])
    plans = [plan_for(v, host=host, log_level=log_level) for v in chosen]
    if params.get("dry_run"):
        return SkillResult(ok=True, value={"plan": plans}, actions_taken=_lines(plans, "would run"))

    Supervisor = _supervisor_cls()
    started = []
    for p in plans:
        sup = Supervisor(
            p["argv"],
            env={**os.environ, **p["env"]},
            host=host,
            port=p["port"],
            drain_s=float(params.get("drain_s", 30.0)),
            ready_timeout_s=float(params.get("ready_timeout_s", 300.0)),
            subject_prefix=f"app:{p['name']}",
        )
        try:
            sup.start()
        except (OSError, RuntimeError) as exc:
            for other in reversed(started):
                other.stop()
            return SkillResult(ok=False, errors=[f"app run: variant {p['name']} did not start: {exc}"])
        started.append(sup)
        ctx.logger.info("variant %s serving (supervised) on %s", p["name"], p["url"])
    _run_group()(started)
    return SkillResult(ok=True, value={"plan": plans}, actions_taken=_lines(plans, "ran"))


__all__ = ["plan_for", "run", "variants"]
