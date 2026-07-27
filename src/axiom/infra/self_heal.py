# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Self-healing CLI — recovery strategies and bus emission.

When a CLI command crashes due to argument parsing or dispatch errors,
this module:
1. Attempts command-specific recovery (e.g., coalescing split args)
2. Emits the error as a signal on the EventBus for downstream subscribers

Recovery strategies are registered per-command. The bus handles durable
JSONL logging and subscriber dispatch (GitLab issue filing, etc.).
"""

from __future__ import annotations

import os
import platform
import traceback as tb_module
from argparse import Namespace
from collections.abc import Callable
from datetime import UTC, datetime

from axiom.infra.bus import EventBus
from axiom.infra.hash_utils import MEDIUM, fingerprint

# ---------------------------------------------------------------------------
# Recovery registry
# ---------------------------------------------------------------------------

# command_name -> list of strategy callables
# Each strategy: (args: Namespace, error: Exception) -> Namespace | None
RecoveryStrategy = Callable[[Namespace, Exception], Namespace | None]

RECOVERY_STRATEGIES: dict[str, list[RecoveryStrategy]] = {}


def register_recovery(command: str, strategy: RecoveryStrategy) -> None:
    """Register a recovery strategy for a CLI command."""
    RECOVERY_STRATEGIES.setdefault(command, []).append(strategy)


def attempt_recovery(
    command: str, args: Namespace, error: Exception,
) -> Namespace | None:
    """Try registered recovery strategies for a command.

    Returns a fixed Namespace if recovery succeeded, None otherwise.
    Strategies are tried in registration order; first success wins.
    """
    for strategy in RECOVERY_STRATEGIES.get(command, []):
        try:
            result = strategy(args, error)
            if result is not None:
                return result
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Built-in strategies
# ---------------------------------------------------------------------------

def _recover_brief_since(args: Namespace, error: Exception) -> Namespace | None:
    """Coalesce args.since + args.topic when _parse_since fails.

    When the user types: axi signal brief --since last week
    argparse sees: since="last", topic="week"
    _parse_since("last") raises ValueError.

    Fix: join since + topic into a single since value, clear topic.
    """
    since = getattr(args, "since", None)
    topic = getattr(args, "topic", None)

    if not since or not topic:
        return None

    # Only recover ValueError from _parse_since
    if not isinstance(error, ValueError):
        return None
    if "time expression" not in str(error).lower() and "unrecognized" not in str(error).lower():
        return None

    # Try the combined value
    combined = f"{since} {topic}"
    try:
        from axiom.extensions.builtins.signals.briefing import _parse_since
        _parse_since(combined)  # Validate it parses
    except (ValueError, ImportError):
        return None

    # Build fixed args
    fixed = Namespace(**vars(args))
    fixed.since = combined
    fixed.topic = None
    return fixed


register_recovery("brief", _recover_brief_since)


# ---------------------------------------------------------------------------
# Connection-aware recovery
# ---------------------------------------------------------------------------

def _is_connection_error(error: Exception) -> bool:
    """Check if an error is caused by a connection failure, not a code bug."""
    error_str = str(error).lower()
    error_type = type(error).__name__

    connection_indicators = [
        "connection refused", "connection reset", "connection timed out",
        "timeout", "timed out", "rate limit", "429", "503", "502",
        "name or service not known", "nodename nor servname",
        "ssl", "certificate", "handshake",
        "unauthorized", "401", "403",
    ]
    connection_types = [
        "ConnectionError", "ConnectionRefusedError", "ConnectionResetError",
        "TimeoutError", "HTTPError", "URLError", "SSLError",
    ]

    if error_type in connection_types:
        return True
    return any(indicator in error_str for indicator in connection_indicators)


def _recover_connection_error(args: Namespace, error: Exception) -> Namespace | None:
    """Try to fix connection errors by ensuring services are running.

    Instead of patching code (TRIAGE's normal path), check if the failure
    is a connection issue and try to remediate it directly.
    """
    if not _is_connection_error(error):
        return None

    try:
        from axiom.infra.connections import ensure_available, get_registry

        registry = get_registry()
        # Try to ensure all connections are available
        for conn in registry.all():
            if conn.ensure_module and conn.ensure_function:
                ensure_available(conn.name, registry=registry)

        # Return original args to retry the command
        return args

    except Exception:
        return None


# Register for all commands — connection errors can happen anywhere
for _cmd in ["signal", "brief", "chat", "pub", "doc", "rag", "connect", "status"]:
    register_recovery(_cmd, _recover_connection_error)


# ---------------------------------------------------------------------------
# Bus emission
# ---------------------------------------------------------------------------

def _fingerprint(command: str, error: Exception) -> str:
    """Stable fingerprint for dedup — same command + error type + message pattern."""
    raw = f"{command}:{type(error).__name__}:{str(error)}"
    return fingerprint(raw, length=MEDIUM)


def _collect_environment() -> dict[str, str]:
    """Collect environment context for support diagnostics."""
    env: dict[str, str] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cwd": os.getcwd(),
    }
    try:
        from importlib.metadata import version

        from axiom.infra.branding import get_branding
        env["neut_version"] = version(get_branding().package_name)
    except Exception:
        env["neut_version"] = "unknown"
    return env


def emit_cli_error(
    bus: EventBus,
    command: str,
    argv: list[str],
    error: Exception,
    recovered: bool,
    traceback_str: str = "",
) -> None:
    """Publish a CLI error as a signal on the event bus.

    The JSONL log is the durable record — always written, even when no
    issue tracker is configured. A downstream agent can read
    cli_events.jsonl and package support requests from it.

    Args:
        bus: EventBus instance (writes to cli_events.jsonl).
        command: The sense subcommand that failed (e.g., "brief").
        argv: Full sys.argv at time of error.
        error: The caught exception.
        recovered: Whether a recovery strategy fixed the error.
        traceback_str: Pre-formatted traceback string. If empty, captures
            the current exception traceback automatically.
    """
    # Capture traceback if not provided
    if not traceback_str:
        traceback_str = tb_module.format_exc()
        # format_exc returns "NoneType: None\n" when no exception is active
        if "NoneType" in traceback_str:
            traceback_str = "".join(
                tb_module.format_exception(type(error), error, error.__traceback__)
            )

    bus.publish("cli.arg_error", {
        "command": command,
        "argv": argv,
        "error_type": type(error).__name__,
        "error_message": str(error),
        "traceback": traceback_str,
        "fingerprint": _fingerprint(command, error),
        "recovered": recovered,
        "environment": _collect_environment(),
        "timestamp": datetime.now(UTC).isoformat(),
    }, source="axiom_cli")
