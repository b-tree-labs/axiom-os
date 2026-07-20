# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chat-time federation re-probe nudge.

Hooks into the chat-startup path (called from `render_welcome` /
chat CLI entry) and surfaces a one-line tip when a richer remote
LLM is reachable but the operator hasn't adopted it.

Closes the "chat finds a self-hosted node and suggests it" gap. Built on
top of `axiom.setup.federation_probe` (the install-time probe
primitive from PR #227) — chat reuses the discovery, adoption
check, and decline memo so the operator sees consistent state
across install-time and chat-time.

Safety:
  - TTY-only (silent in CI / piped output)
  - Short timeout on probe — never blocks chat startup
  - Catches every exception — chat MUST start even if federation is
    unreachable / misconfigured / permission-denied
  - Respects the install-time decline memo so the prompt doesn't
    nag operators who already said no
  - Prints at most one tip even when many candidates are reachable
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from axiom.setup.federation_probe import ProbeResult


def _is_tty() -> bool:
    try:
        return bool(sys.stdout.isatty()) and bool(sys.stdin.isatty())
    except Exception:
        return False


def _llm_providers_path() -> Path:
    """Where the operator's adopted-providers file lives. Override
    via test-side monkeypatch."""
    from axiom.infra.paths import get_runtime_config_dir
    return get_runtime_config_dir() / "llm-providers.toml"


def _already_adopted(conn_name: str) -> bool:
    """True if `conn_name` is already present in llm-providers.toml.

    Defensive: missing file / parse error / no providers block → False
    (don't suppress the nudge on a file-read hiccup).
    """
    path = _llm_providers_path()
    if not path.exists():
        return False
    try:
        # tomllib stdlib in 3.11+; this codebase supports that floor.
        import tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    gateway = data.get("gateway") or {}
    providers = gateway.get("providers") or []
    for p in providers:
        if p.get("name") == conn_name:
            return True
    return False


def _has_declined(conn_name: str) -> bool:
    """Thin wrapper so tests can monkeypatch this module's seam
    rather than reaching into `axiom.setup.federation_probe`."""
    try:
        from axiom.setup.federation_probe import has_declined
        return bool(has_declined(conn_name))
    except Exception:
        return False


def _discover() -> "list[ProbeResult]":
    """Discover reachable llm-category federation endpoints.

    Wrapped in its own seam so tests can stub it. Real impl creates
    a fresh ConnectionRegistry and probes; the probe itself uses
    `check_health` with a built-in timeout, so this returns quickly
    even when nothing is reachable.
    """
    from axiom.infra.connections import ConnectionRegistry
    from axiom.setup.federation_probe import discover_llm_endpoints

    registry = ConnectionRegistry()
    try:
        registry.discover_from_extensions()
    except Exception:
        return []
    return discover_llm_endpoints(registry)


def maybe_render_federation_nudge() -> None:
    """Pre-REPL nudge: one-line tip if a richer reachable LLM exists.

    No-op when:
      - stdin/stdout is not a TTY
      - probe finds no reachable candidates
      - the only reachable candidates are already adopted
        (in llm-providers.toml) OR were previously declined
        (federation_declined.json from PR #227)
      - any unhandled exception during probe (never block chat startup)
    """
    if not _is_tty():
        return

    try:
        candidates = _discover()
    except Exception:
        return
    if not candidates:
        return

    # Filter: not already adopted, not previously declined.
    usable: list = []
    for result in candidates:
        name = getattr(result.connection, "name", "")
        if not name:
            continue
        if _already_adopted(name):
            continue
        if _has_declined(name):
            continue
        usable.append(result)
    if not usable:
        return

    # Surface the first one. Multiple reachable providers shouldn't
    # spam the welcome banner — one is enough to prompt action.
    pick = usable[0]
    name = pick.connection.name
    display = getattr(pick.connection, "display_name", name) or name

    from axiom.infra.branding import get_branding
    cli = (get_branding().cli_name or "axi").strip()

    print(
        f"💡 Tip: {display} is reachable. "
        f"Adopt it with: {cli} federation discover"
    )
