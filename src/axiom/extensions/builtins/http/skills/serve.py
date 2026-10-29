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

    bound_note = _bound_preflight(params, host)
    if isinstance(bound_note, SkillResult):
        return bound_note

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

    for line in bound_note:
        print(line, file=sys.stderr)

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
    return SkillResult(ok=True, actions_taken=[*bound_note, f"served on {host}:{port}"])


_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _bound_preflight(params: dict[str, Any], host: str) -> list[str] | SkillResult:
    """ADR-207: ``--bound`` refuses unless bound, binds loopback, and says where data comes from.

    Without ``--bound`` a bound install is still warned when it serves beyond loopback, because
    the binding's key is attached to every forwarded read whoever asks. Never prints the key.
    """
    from axiom.infra.serving_mode import probe, resolve_mode

    mode = resolve_mode()
    exposed = host not in _LOOPBACK
    lines: list[str] = []
    if params.get("bound"):
        if not mode.bound:
            return SkillResult(ok=False, errors=[
                f"--bound needs a current site binding on an install that does not hold the "
                f"medallion; this install is {mode.mode} ({mode.reason}). Bind first (site bind "
                f"<site>), or run without --bound."])
        if exposed and not params.get("expose"):
            return SkillResult(ok=False, errors=[
                f"--bound serves on loopback only: --host {host} would let anyone who reaches "
                f"this port read {mode.site} with the binding's key. Use --host 127.0.0.1, or "
                f"add --expose if that is intended."])
        node = probe(mode)
        state = "reachable" if node.reachable else f"UNREACHABLE: {node.error}"
        lines.append(f"bound: {mode.site} at {mode.node} (key for {mode.key_site}"
                     + (f", viewing as {mode.view_as}" if mode.view_as else "")
                     + f"); node {node.version or 'version unknown'}, {state}")
    if mode.bound and exposed:
        lines.append(f"WARNING: serving a bound install on {host}: every forwarded read carries the "
                     f"binding's key for {mode.key_site}, whoever sends it.")
    return lines


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
