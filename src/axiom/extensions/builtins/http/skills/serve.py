# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``serve.run`` — compose and launch the one HTTP app (spec-serve §7).

The skill function ``(params, ctx) -> SkillResult`` per ADR-056. The CLI
verb ``axi serve`` is a thin wrapper that translates flags → params and
dispatches here; an agent persona reaches the same surface.

``--list`` returns the route table in the ``SkillResult`` without binding
a socket (SRV-013). Otherwise the composed app is run via ``run_server``,
which keeps the uvicorn signal-handler guard so the CLI owns Ctrl-C /
SIGTERM (SRV-012).
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    host = params.get("host", "127.0.0.1")
    port = int(params.get("port", 8787))
    profile = params.get("profile")
    log_level = params.get("log_level", "warning")
    list_only = bool(params.get("list", False))
    insecure = bool(params.get("insecure", False))

    try:
        from ..compose import compose_app, route_table
    except ImportError as exc:  # pragma: no cover — missing serve extra
        return SkillResult(
            ok=False,
            errors=[_missing_deps_message(exc)],
        )

    if list_only:
        table = route_table(profile=profile)
        return SkillResult(
            ok=True,
            value={
                "routes": [
                    {
                        "prefix": e.prefix,
                        "extension": e.extension,
                        "requires_authz": e.requires_authz,
                        "trust_zone": e.trust_zone,
                    }
                    for e in table
                ]
            },
            actions_taken=[f"composed {len(table)} route(s) (not bound)"],
        )

    from ..server import run_server

    app = compose_app(profile=profile, allow_insecure=insecure)
    if insecure:
        ctx.logger.warning(
            "serving with --insecure: auth-required mounts run WITHOUT authz "
            "enforcement (dev/loopback only)")
    ctx.logger.info("serving on http://%s:%s (profile=%s)", host, port, profile)
    run_server(app, host=host, port=port, log_level=log_level)
    return SkillResult(ok=True, actions_taken=[f"served on {host}:{port}"])


def _missing_deps_message(exc: Exception) -> str:
    """Legible diagnose when the serve extra is absent (SRV-051)."""
    return (
        "The 'serve' HTTP substrate needs fastapi + uvicorn.\n"
        "Install with:  pip install 'axiom-os-lm[serve]'\n"
        f"(underlying import error: {exc})"
    )


__all__ = ["run"]
