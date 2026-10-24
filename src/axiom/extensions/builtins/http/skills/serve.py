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

import os
import sys
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
                        "governs": list(e.governs),
                    }
                    for e in table
                ]
            },
            actions_taken=[f"composed {len(table)} route(s) (not bound)"],
        )

    if params.get("supervise") and "AXI_LISTEN_FD" not in os.environ:
        return _supervise(params, ctx, host=host, port=port, profile=profile)

    # Manifest-declared service mounts from every discoverable extension
    # — including pip-installed packages — so a bare node (or a
    # separate-instance consumer's node) composes its full surface with
    # no site-repo hand-registration.
    from ..compose import discovered_manifests
    from ..server import run_server

    app = compose_app(profile=profile, allow_insecure=insecure,
                      bind_host=host, manifests=discovered_manifests())
    if insecure:
        ctx.logger.warning(
            "serving with --insecure: auth-required mounts run WITHOUT authz "
            "enforcement (dev/loopback only)")
    ctx.logger.info("serving on http://%s:%s (profile=%s)", host, port, profile)
    run_server(app, host=host, port=port, log_level=log_level)
    return SkillResult(ok=True, actions_taken=[f"served on {host}:{port}"])


def _supervise(
    params: dict[str, Any], ctx: SkillContext, *, host: str, port: int, profile: str | None
) -> SkillResult:
    """Own the socket and run the app as a replaceable child (ADR-182 D3).

    The child is this same command without ``--supervise``; it sees
    ``AXI_LISTEN_FD`` and accepts on the supervisor's socket. SIGHUP replaces
    it (systemd: ``ExecReload=/bin/kill -HUP $MAINPID``); SIGTERM drains it and
    exits. ``AXI_SERVE_PYTHON`` names the interpreter for new copies — point
    it at an updater's ``current`` link so a switch after an update runs the
    new version.
    """
    from axiom.infra.switch import Supervisor

    drain_s = float(params.get("drain_s", 30.0))
    argv = [
        os.environ.get("AXI_SERVE_PYTHON", sys.executable),
        "-m",
        "axiom.extensions.builtins.http.cli",
        "--log-level",
        str(params.get("log_level", "warning")),
    ]
    if profile:
        argv += ["--profile", profile]
    if params.get("insecure"):
        argv.append("--insecure")
    sup = Supervisor(
        argv,
        host=host,
        port=port,
        drain_s=drain_s,
        ready_timeout_s=float(params.get("ready_timeout_s", 300.0)),
        subject_prefix=f"serve:{profile or 'default'}",
    )
    sup.start()
    ctx.logger.info("serving (supervised) on http://%s:%s (profile=%s)", host, sup.port, profile)
    sup.run_forever()
    return SkillResult(ok=True, actions_taken=[f"served (supervised) on {host}:{sup.port}"])


def _missing_deps_message(exc: Exception) -> str:
    """Legible diagnose when the serve extra is absent (SRV-051)."""
    return (
        "The 'serve' HTTP substrate needs fastapi + uvicorn.\n"
        "Install with:  pip install 'axiom-os-lm[serve]'\n"
        f"(underlying import error: {exc})"
    )


__all__ = ["run"]
