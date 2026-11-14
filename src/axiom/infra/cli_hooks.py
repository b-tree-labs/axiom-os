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


def _brand_cli() -> str:
    """The command the operator actually typed.

    These lines said "axi" unconditionally, so a consumer distribution's CLI
    told the operator to run a command that does not exist on their machine.
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"

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
#: How much of a diagnosis summary belongs in the one-line pointer. The rest
#: is one command away.
_SUMMARY_WIDTH = 96

# Pending-diagnoses surface.  TRIAGE's CLI listener
# (`extensions/builtins/diagnostics/cli_listener.py`) files a pending
# diagnosis whenever a `cli.arg_error` event matches a known pattern.  They
# are read on purpose, on the triage surface: `axi triage pending` lists them
# in full, and the other `axi triage` verbs print a one-line pointer.
#
# This used to print in front of EVERY command, `--help` included, and it was
# the first thing anyone saw of `axi`: sixteen diagnoses from weeks earlier,
# seven of them test fixtures, ahead of an unrelated command (#1520). A queue
# nobody acted on became a preamble everybody learned to skip. A diagnosis
# belongs to the failure that filed it and to the surface that exists to read
# it, not to whatever the operator typed next. Opt-out on the triage surface
# too via `AXI_DIAGNOSES_QUIET=1`.
# ---------------------------------------------------------------------------

#: `axi triage` verbs that do not get the pointer: `pending` prints the queue
#: itself, `clear` manages it, and `heartbeat` is the daemon path, whose
#: stderr is a log nobody is reading interactively.
_TRIAGE_VERBS_WITHOUT_POINTER = frozenset({"pending", "clear", "heartbeat"})


def surface_pending_diagnoses(command: str = "") -> None:
    """Print a short pointer to pending TRIAGE diagnoses, on the triage surface only.

    ``command`` is the command about to run: the subcommand, optionally
    followed by its first argument (``"triage"``, ``"triage sweep"``). Every
    command that is not ``triage`` prints nothing, so the queue never stands
    in front of somebody else's output. Stale entries are expired first (see
    :func:`cli_listener.expire_stale`).

    Soft-fails on every error path: a corrupt log or an import error must
    never block the command the operator actually asked for.
    """
    if os.environ.get("AXI_DIAGNOSES_QUIET"):
        return
    words = command.split()
    if words[:1] != ["triage"]:
        return
    if len(words) > 1 and words[1] in _TRIAGE_VERBS_WITHOUT_POINTER:
        return
    try:
        from axiom.extensions.builtins.diagnostics import cli_listener

        # Fold in anything filed at an older, wider scope (a brand home, or the
        # machine-wide path) so those failures are not stranded somewhere no
        # install will read them again, then drop what has aged out.
        cli_listener._adopt_legacy_diagnoses()
        cli_listener.expire_stale()
        # A diagnosis whose trigger no longer reproduces retires itself (#812).
        cli_listener.retire_resolved()
        pending = cli_listener.read_pending()
    except Exception as exc:  # noqa: BLE001
        log.debug("surface_pending_diagnoses: read failed: %s", exc)
        return
    if not pending:
        return
    # One line per diagnosis; the remedy stays one command away, where it can
    # be read on purpose rather than scrolled past.
    count = len(pending)
    print(
        f"\n[TRIAGE] {count} pending diagnos{'is' if count == 1 else 'es'} "
        f"from earlier commands:",
        file=sys.stderr,
    )
    for d in pending:
        fp = d.get("fingerprint", "?")
        summary = " ".join(str(d.get("summary", "")).split())
        if len(summary) > _SUMMARY_WIDTH:
            summary = summary[: _SUMMARY_WIDTH - 1].rstrip() + "…"
        confidence = d.get("confidence", 0.0)
        print(f"  • {fp}  {summary} ({confidence:.2f})", file=sys.stderr)
    first = pending[0].get("fingerprint", "<fingerprint>")
    print(
        f"  Read the remedy: {_brand_cli()} triage pending   ·   Dismiss: {_brand_cli()} triage clear {first}",
        file=sys.stderr,
    )
    print(
        "  Silence them for one command with AXI_DIAGNOSES_QUIET=1\n",
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
