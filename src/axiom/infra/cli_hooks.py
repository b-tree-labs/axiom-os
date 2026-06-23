# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI lifecycle observer events.

Published by ``axiom_cli.main`` at the top-level command dispatch entry
and exit per spec §8c. Observer-only — the platform doesn't allow CLI
commands to be denied by an interceptor (that would push policy into
the dispatcher; classification gates belong upstream of CLI invocation
or downstream at the tool gateway).

For ``extension.pre_install`` / ``extension.post_install`` / ``federation.pre_accept``
/ ``federation.post_accept``: per spec §8 closing paragraph, these fire
when their consumer code is touched. v1 stubs publish the events but
no-op at the install / receive code path so observers can subscribe; the
real wiring lands when the extension installer and federation receive
loop next get touched (TODO markers preserved at those call-sites).
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

from axiom.infra.bus import EventBus

log = logging.getLogger("axiom.infra.cli_hooks")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def publish_command_started(
    *,
    command_path: str,
    args: list[str],
    principal: str,
    eventbus: EventBus | None,
) -> None:
    """Fire ``cli.command_started``. Soft-fails when no bus is wired."""
    if eventbus is None:
        return
    try:
        eventbus.publish(
            "cli.command_started",
            {
                "command_path": command_path,
                "args": list(args),
                "principal": principal,
                "started_at": _now_iso(),
            },
            source="cli",
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("cli.command_started publish failed: %s", exc)


def publish_command_ended(
    *,
    command_path: str,
    exit_code: int,
    duration_ms: int,
    eventbus: EventBus | None,
) -> None:
    """Fire ``cli.command_ended``. Soft-fails when no bus is wired."""
    if eventbus is None:
        return
    try:
        eventbus.publish(
            "cli.command_ended",
            {
                "command_path": command_path,
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "ended_at": _now_iso(),
            },
            source="cli",
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("cli.command_ended publish failed: %s", exc)


# ---------------------------------------------------------------------------
# Extension install stubs — per spec §8 closing paragraph, the platform
# fires these but no-ops at the consumer call-site in v1. Real wiring
# lands with the next pass at the install / federation-receive code.
# ---------------------------------------------------------------------------


def publish_extension_pre_install(
    *,
    name: str,
    version: str,
    manifest: dict[str, Any],
    signature: str,
    source_url: str,
    eventbus: EventBus | None,
) -> None:
    """Fire ``extension.pre_install``. Stub call-site for v1.

    TODO(hooks): wire from `axi ext install` when the installer next
    gets touched. v1 publishes but isn't called from any path yet.
    """
    if eventbus is None:
        return
    try:
        eventbus.publish(
            "extension.pre_install",
            {
                "name": name,
                "version": version,
                "manifest": manifest,
                "signature": signature,
                "source_url": source_url,
            },
            source="ext_installer",
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("extension.pre_install publish failed: %s", exc)


def publish_extension_post_install(
    *,
    name: str,
    version: str,
    install_path: str,
    manifest: dict[str, Any],
    eventbus: EventBus | None,
) -> None:
    """Fire ``extension.post_install``. Stub call-site for v1."""
    if eventbus is None:
        return
    try:
        eventbus.publish(
            "extension.post_install",
            {
                "name": name,
                "version": version,
                "install_path": install_path,
                "manifest": manifest,
            },
            source="ext_installer",
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("extension.post_install publish failed: %s", exc)


def publish_federation_pre_accept(
    *,
    message: dict[str, Any],
    peer_principal: str,
    classification: str,
    signature_chain: list[dict[str, Any]],
    eventbus: EventBus | None,
) -> None:
    """Fire ``federation.pre_accept``. Stub for v1.

    TODO(hooks): wire from `axiom.vega.federation.receive` (or current
    equivalent) when the federation receive loop gets touched. The vega
    extraction (ADR-031) is in-flight; rather than fork the receive code
    path now, we ship the stub and the wire-up lands with the next vega
    pass.
    """
    if eventbus is None:
        return
    try:
        eventbus.publish(
            "federation.pre_accept",
            {
                "message": message,
                "peer_principal": peer_principal,
                "classification": classification,
                "signature_chain": signature_chain,
            },
            source="federation",
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("federation.pre_accept publish failed: %s", exc)


def publish_federation_post_accept(
    *,
    message: dict[str, Any],
    peer_principal: str,
    accepted_at: str,
    eventbus: EventBus | None,
) -> None:
    """Fire ``federation.post_accept``. Stub for v1."""
    if eventbus is None:
        return
    try:
        eventbus.publish(
            "federation.post_accept",
            {
                "message": message,
                "peer_principal": peer_principal,
                "accepted_at": accepted_at,
            },
            source="federation",
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("federation.post_accept publish failed: %s", exc)


# ---------------------------------------------------------------------------
# Pre-command pending-diagnoses surface.  TRIAGE's CLI listener
# (`extensions/builtins/diagnostics/cli_listener.py`) records pending
# diagnoses to `~/.axi/agents/triage/pending-diagnoses.jsonl` whenever a
# `cli.arg_error` event matches a known pattern.  The next CLI invocation
# calls `surface_pending_diagnoses()` from `axiom_cli.main` *before*
# dispatch, so the user sees the remedy before re-running the broken
# command.  Opt-out via `AXI_DIAGNOSES_QUIET=1`.
#
# This closes the loop the user described 2026-05-03: "triage should have
# seen the error and... a remedy that the user would be prompted for the
# next time they ran a cli command."
# ---------------------------------------------------------------------------


def surface_pending_diagnoses() -> None:
    """Print pending TRIAGE diagnoses to stderr, if any.

    Called from `axiom_cli.main` at the top of dispatch.  Soft-fails on
    every error path — a corrupt log or import error must NEVER block
    the user's actual command from running.
    """
    if os.environ.get("AXI_DIAGNOSES_QUIET"):
        return
    try:
        from axiom.extensions.builtins.diagnostics import cli_listener
        pending = cli_listener.read_pending()
    except Exception as exc:  # noqa: BLE001
        log.debug("surface_pending_diagnoses: read failed: %s", exc)
        return
    if not pending:
        return
    print(
        f"\n[TRIAGE] {len(pending)} pending diagnos{'is' if len(pending) == 1 else 'es'} "
        f"from prior CLI failure{'s' if len(pending) > 1 else ''}:",
        file=sys.stderr,
    )
    for d in pending:
        fp = d.get("fingerprint", "?")
        summary = d.get("summary", "")
        remedy = d.get("remedy", "")
        confidence = d.get("confidence", 0.0)
        print(f"  • [{fp}] (confidence {confidence:.2f}) {summary}", file=sys.stderr)
        if remedy:
            # Indent the remedy so it's visually associated with the bullet.
            for line in remedy.splitlines():
                print(f"      {line}", file=sys.stderr)
    print(
        "  Acknowledge with: axi triage clear <fingerprint>  "
        "(or AXI_DIAGNOSES_QUIET=1 to silence)\n",
        file=sys.stderr,
    )


__all__ = [
    "publish_command_ended",
    "publish_command_started",
    "publish_extension_post_install",
    "publish_extension_pre_install",
    "publish_federation_post_accept",
    "publish_federation_pre_accept",
    "surface_pending_diagnoses",
]
