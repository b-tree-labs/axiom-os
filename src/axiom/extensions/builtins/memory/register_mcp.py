# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Register the axiom-memory MCP server in each LLM tool's user-scope config.

Thin adapters over the **one registrar matrix** in
:mod:`axiom.extensions.builtins.mcp.install` (:data:`TOOL_SPECS`). That
module owns every harness's config path, file format, detection rule and
EC posture; this one supplies the memory server's command/args and the
per-tool result shapes that ``axi memory register-mcp`` and ``axi doctor``
consume. Nothing here parses or writes a config format: a harness that
:data:`TOOL_SPECS` knows is registrable for the memory server by
construction, and one it does not know cannot be registered here either
(see :data:`REGISTRAR_ONLY` for the single documented exception).

Per-tool registrar protocol: each tool gets a :class:`ToolRegistrar`
declaring three callables — ``detect`` (is it installed?), ``register``
(write the entry idempotently), ``is_registered`` (read-only check of the
file the writer wrote). ``axi memory register-mcp --all`` walks the
registry, calling ``register`` on every tool that ``detect`` reports True
for.

Public API (names and return shapes are stable; doctor and the CLI rely on
them):

- :data:`TOOL_REGISTRARS` — the registry: ``{tool_name: ToolRegistrar}``.
- :data:`REGISTRAR_ONLY` — tools registered here but absent from
  :data:`TOOL_SPECS`, with the reason documented at the definition.
- :func:`resolve_memory_server` — ``(server_name, command, args)``.
- :func:`detect_installed_tools` — ``{tool_name: bool}``.
- :func:`register_all_detected` — one command registers every detected tool.
- ``register_<tool>_mcp`` / ``is_<tool>_mcp_registered`` per tool;
  :func:`register_axiom_memory_mcp` / :func:`is_axiom_memory_mcp_registered`
  are the Claude Code pair under their original names.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.mcp.install import (
    TOOL_SPECS,
    ToolSpec,
    config_path_for,
    install_entry,
    read_entry,
    spec_for,
)

# The MCP entry-point is stable across worktrees + install modes: ``-m`` lets
# the interpreter resolve the module, so one registration shape serves a PyPI
# wheel and an editable checkout alike (the same rule as
# ``mcp.install.resolve_server``).
_MEMORY_SERVER_MODULE = "axiom.extensions.builtins.memory.mcp_server"
_MCP_ARGS: list[str] = ["-m", _MEMORY_SERVER_MODULE]
_SERVER_KEY = "axiom-memory"


def _pinned_python(home: Path | None = None) -> str:
    """The interpreter a registration should point at.

    Prefers the pinned, non-editable service venv. The workspace venv is
    an editable install anchored to a worktree; when that anchor moved on
    2026-07-24 every axiom entry point broke and capture died silently
    for three weeks. A registration that outlives worktree churn is worth
    more than one that happens to match the current shell.
    """
    base = Path(home) if home is not None else Path.home()
    pinned = base / ".axi" / "service-venv" / "bin" / "python"
    if pinned.exists():
        return str(pinned)
    return sys.executable


def resolve_memory_server(python_path: str | None = None) -> tuple[str, str, list[str]]:
    """Return ``(server_name, command, args)`` for the memory MCP server.

    ``command`` is ``python_path`` when given, else the pinned service-venv
    interpreter when present, else ``sys.executable``. ``args`` is always
    the ``-m`` form — never a filesystem path into a checkout — so a
    registration written from an editable install survives the worktree
    moving, and one written from a PyPI install has the identical shape.
    Doctor and ``--check`` measure staleness against this same command, so
    a deliberately pinned registration is never reported stale.
    """
    return _SERVER_KEY, python_path or _pinned_python(), list(_MCP_ARGS)


# ===========================================================================
# Spec-backed adapters — one register / is_registered pair per matrix row
# ===========================================================================


def _resolve_path(spec: ToolSpec, config_path: Path | str | None) -> Path:
    return Path(config_path) if config_path else config_path_for(spec)


def _is_stale(command: str, expected_command: str | None) -> bool:
    if expected_command is None or command == expected_command:
        return False
    try:
        return os.path.realpath(command) != os.path.realpath(expected_command)
    except OSError:
        return True


def _register_via_spec(
    tool: str, *, config_path: Path | str | None, python_path: str | None,
) -> dict[str, Any]:
    """Write the memory server into ``tool``'s config through its spec.

    Raises (``ValueError`` or a parser error) on a config it cannot parse,
    before writing anything — a harness config is the user's, and clobbering
    it to add a memory server is the wrong trade.
    """
    spec = spec_for(tool)
    assert spec is not None, tool  # guarded at registrar construction
    name, command, args = resolve_memory_server(python_path)
    path = _resolve_path(spec, config_path)
    action = install_entry(spec, name, command, args, {}, path=path)
    return {"action": action, "command": command, "config_path": str(path), "tool": tool}


def _is_registered_via_spec(
    tool: str, *, config_path: Path | str | None, expected_command: str | None,
) -> dict[str, Any]:
    """Read back the memory server's entry from ``tool``'s config file.

    ``reason`` is ``missing`` (no file, or no entry) or ``malformed_config``
    (file present but unparseable — never rewritten). ``stale`` compares the
    recorded command with ``expected_command`` through ``realpath``.
    """
    spec = spec_for(tool)
    assert spec is not None, tool
    path = _resolve_path(spec, config_path)
    base = {"config_path": str(path), "tool": tool}
    if not path.exists():
        return {"registered": False, "reason": "missing", **base}
    try:
        entry = read_entry(spec, _SERVER_KEY, path=path)
    except Exception as exc:  # noqa: BLE001 — any parser failure is "malformed"
        return {"registered": False, "reason": "malformed_config", "detail": str(exc), **base}
    if entry is None:
        return {"registered": False, "reason": "missing", **base}
    command = str(entry.get("command", ""))
    return {
        "registered": True,
        "command": command,
        "stale": _is_stale(command, expected_command),
        **base,
    }


def _spec_registrar(
    tool: str,
) -> tuple[Callable[..., dict[str, Any]], Callable[..., dict[str, Any]]]:
    """Build the ``register`` / ``is_registered`` pair for one matrix row.

    Refuses a tool the matrix does not know: the memory server can only be
    registered where the aggregation server can, by construction.
    """
    if spec_for(tool) is None:
        raise KeyError(f"{tool!r} is not a row of mcp.install.TOOL_SPECS")

    def register(
        *, config_path: Path | None = None, python_path: str | None = None,
    ) -> dict[str, Any]:
        return _register_via_spec(tool, config_path=config_path, python_path=python_path)

    def is_registered(
        *, config_path: Path | None = None, expected_command: str | None = None,
    ) -> dict[str, Any]:
        return _is_registered_via_spec(
            tool, config_path=config_path, expected_command=expected_command,
        )

    ident = tool.replace("-", "_")
    register.__name__ = f"register_{ident}_mcp"
    register.__qualname__ = register.__name__
    register.__doc__ = (
        f"Write or update the {_SERVER_KEY} entry in the {tool} user-scope config. "
        "Idempotent; returns {action, command, config_path, tool}."
    )
    is_registered.__name__ = f"is_{ident}_mcp_registered"
    is_registered.__qualname__ = is_registered.__name__
    is_registered.__doc__ = (
        f"Read-only: is {_SERVER_KEY} registered in the {tool} config, and is its "
        "command stale? Returns {registered, command?, stale?, reason?, config_path, tool}."
    )
    return register, is_registered


# One pair per matrix row. The Claude Code pair keeps its original names
# (``register_axiom_memory_mcp`` / ``is_axiom_memory_mcp_registered``) because
# doctor and the CLI import them by those names.
register_axiom_memory_mcp, is_axiom_memory_mcp_registered = _spec_registrar("claude-code")
register_claude_code_mcp, is_claude_code_mcp_registered = (
    register_axiom_memory_mcp, is_axiom_memory_mcp_registered,
)
register_claude_desktop_mcp, is_claude_desktop_mcp_registered = _spec_registrar("claude-desktop")
register_cursor_mcp, is_cursor_mcp_registered = _spec_registrar("cursor")
register_windsurf_mcp, is_windsurf_mcp_registered = _spec_registrar("windsurf")
register_gemini_mcp, is_gemini_mcp_registered = _spec_registrar("gemini")
register_hermes_mcp, is_hermes_mcp_registered = _spec_registrar("hermes")
register_opencode_mcp, is_opencode_mcp_registered = _spec_registrar("opencode")
register_vscode_mcp, is_vscode_mcp_registered = _spec_registrar("vscode")
register_codex_mcp, is_codex_mcp_registered = _spec_registrar("codex")

# Detection is the spec's rule, re-exported for callers that import it here.
_detect_hermes: Callable[[], bool] = spec_for("hermes").detect  # type: ignore[union-attr]


# ===========================================================================
# CLI-driven registrar — OpenClaw (the documented registrar-only exception)
# ===========================================================================
#
# OpenClaw ships its own `mcp add` verb, and its docs disagree with each other
# about whether servers nest under `mcp.servers` or `mcpServers`. Guessing
# wrong either fails silently or corrupts a file the user owns, so until the
# schema is verified from a real install this registrar drives the tool's CLI
# rather than a file writer — which is also why it has no TOOL_SPECS row: the
# matrix describes file formats, and this one is not known.
#
# NAMING: **Clawdbot no longer exists.** It became Moltbot on 2026-01-27 and
# OpenClaw on 2026-01-30 after a trademark dispute, and attackers hijacked the
# abandoned repos and domains during the churn (a fake extension shipped
# malware under the old name). Nothing here reaches for a "clawdbot" path.


def _run_tool(binary: str, args: list[str]) -> tuple[int, str, str]:
    """Run a harness CLI, returning (rc, stdout, stderr). Never raises."""
    import subprocess

    resolved = shutil.which(binary)
    if not resolved:
        return 127, "", "not_installed"
    try:
        proc = subprocess.run(
            [resolved, *args], capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:  # timeout, permissions, anything
        return 1, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _register_via_cli(
    *, binary: str, tool: str, python_path: str, extra: list[str] | None = None,
) -> dict[str, Any]:
    """Add axiom-memory through the harness's own `mcp add` verb."""
    args = [
        "mcp", "add", _SERVER_KEY,
        "--command", python_path,
        *(extra or []),
        # --args swallows the remaining argv, so it must come last.
        "--args", *_MCP_ARGS,
    ]
    rc, _out, err = _run_tool(binary, args)
    if rc != 0:
        return {
            "action": "failed",
            "tool": tool,
            "reason": err.strip() or f"exit_{rc}",
            "error": err.strip(),
            "command": python_path,
        }
    return {"action": "added", "tool": tool, "command": python_path}


def _is_registered_via_cli(*, binary: str, tool: str) -> dict[str, Any]:
    rc, out, err = _run_tool(binary, ["mcp", "list"])
    if rc != 0:
        return {
            "registered": False, "tool": tool,
            "reason": err.strip() or f"exit_{rc}",
        }
    return {"registered": _SERVER_KEY in out, "tool": tool}


def _detect_openclaw() -> bool:
    return bool(shutil.which("openclaw")) or (Path.home() / ".openclaw").exists()


def register_openclaw_mcp(
    *, python_path: str | None = None, **_kwargs: Any,
) -> dict[str, Any]:
    """Register axiom-memory in OpenClaw via its own ``openclaw mcp add``."""
    return _register_via_cli(
        binary="openclaw", tool="openclaw",
        python_path=resolve_memory_server(python_path)[1],
    )


def is_openclaw_mcp_registered(**_kwargs: Any) -> dict[str, Any]:
    return _is_registered_via_cli(binary="openclaw", tool="openclaw")


# ===========================================================================
# Registry
# ===========================================================================


@dataclass(frozen=True)
class ToolRegistrar:
    """Per-tool registrar bundling detect + register + is_registered.

    ``spec`` is the :data:`TOOL_SPECS` row this registrar adapts (``None``
    only for :data:`REGISTRAR_ONLY` tools); ``mechanism`` is ``"file"`` when
    the spec's writer is used and ``"cli"`` when the harness's own command
    is driven.
    """

    name: str
    detect: Callable[[], bool]
    register: Callable[..., dict[str, Any]]
    is_registered: Callable[..., dict[str, Any]]
    spec: ToolSpec | None = None
    mechanism: str = "file"


# Tools registered here without a TOOL_SPECS row. Every entry needs a reason
# in the section above; the parity test fails on any other divergence.
REGISTRAR_ONLY: frozenset[str] = frozenset({"openclaw"})

_SPEC_PAIRS: dict[str, tuple[Callable[..., dict[str, Any]], Callable[..., dict[str, Any]]]] = {
    "claude-code": (register_axiom_memory_mcp, is_axiom_memory_mcp_registered),
    "claude-desktop": (register_claude_desktop_mcp, is_claude_desktop_mcp_registered),
    "cursor": (register_cursor_mcp, is_cursor_mcp_registered),
    "windsurf": (register_windsurf_mcp, is_windsurf_mcp_registered),
    "gemini": (register_gemini_mcp, is_gemini_mcp_registered),
    "hermes": (register_hermes_mcp, is_hermes_mcp_registered),
    "opencode": (register_opencode_mcp, is_opencode_mcp_registered),
    "vscode": (register_vscode_mcp, is_vscode_mcp_registered),
    "codex": (register_codex_mcp, is_codex_mcp_registered),
}


def _build_registry() -> dict[str, ToolRegistrar]:
    registry: dict[str, ToolRegistrar] = {}
    for spec in TOOL_SPECS:
        pair = _SPEC_PAIRS.get(spec.name) or _spec_registrar(spec.name)
        registry[spec.name] = ToolRegistrar(
            name=spec.name,
            detect=spec.detect,
            register=pair[0],
            is_registered=pair[1],
            spec=spec,
            mechanism="file",
        )
    registry["openclaw"] = ToolRegistrar(
        name="openclaw",
        detect=_detect_openclaw,
        register=register_openclaw_mcp,
        is_registered=is_openclaw_mcp_registered,
        spec=None,
        mechanism="cli",
    )
    return registry


TOOL_REGISTRARS: dict[str, ToolRegistrar] = _build_registry()


def detect_installed_tools() -> dict[str, bool]:
    """Return ``{tool_name: detected_bool}`` for every tool in the registry."""
    return {name: reg.detect() for name, reg in TOOL_REGISTRARS.items()}


def register_all_detected(
    python_path: str | None = None,
    *,
    detected: dict[str, bool] | None = None,
) -> dict[str, dict[str, Any]]:
    """Run register on every tool that detect() returns True for.

    Detection runs through :func:`detect_installed_tools` by default;
    callers may supply a pre-computed ``detected`` mapping to override
    (mostly useful for tests).

    Returns a per-tool dict of results. Tools that aren't detected are
    reported as ``{"action": "skipped", "reason": "not_detected"}``. A
    registrar that fails (a config we refuse to rewrite, a harness CLI that
    is missing or errors) is reported as ``{"action": "failed", "reason":
    ...}`` so one bad file never aborts the sweep; a registrar that raises
    ``NotImplementedError`` is reported as ``{"action": "stub"}``.
    """
    _, command, _ = resolve_memory_server(python_path)
    detected = detected if detected is not None else detect_installed_tools()
    results: dict[str, dict[str, Any]] = {}
    for name, reg in TOOL_REGISTRARS.items():
        if not detected.get(name):
            results[name] = {"action": "skipped", "reason": "not_detected", "tool": name}
            continue
        try:
            results[name] = reg.register(python_path=command)
        except NotImplementedError as exc:
            results[name] = {"action": "stub", "reason": str(exc), "tool": name}
        except Exception as exc:  # noqa: BLE001 — per-tool outcome, not a sweep abort
            results[name] = {
                "action": "failed",
                "reason": f"{type(exc).__name__}: {exc}",
                "tool": name,
            }
    return results


__all__ = [
    "REGISTRAR_ONLY",
    "TOOL_REGISTRARS",
    "ToolRegistrar",
    "detect_installed_tools",
    "is_axiom_memory_mcp_registered",
    "is_claude_code_mcp_registered",
    "is_claude_desktop_mcp_registered",
    "is_codex_mcp_registered",
    "is_cursor_mcp_registered",
    "is_gemini_mcp_registered",
    "is_hermes_mcp_registered",
    "is_openclaw_mcp_registered",
    "is_opencode_mcp_registered",
    "is_vscode_mcp_registered",
    "is_windsurf_mcp_registered",
    "register_all_detected",
    "register_axiom_memory_mcp",
    "register_claude_code_mcp",
    "register_claude_desktop_mcp",
    "register_codex_mcp",
    "register_cursor_mcp",
    "register_gemini_mcp",
    "register_hermes_mcp",
    "register_openclaw_mcp",
    "register_opencode_mcp",
    "register_vscode_mcp",
    "register_windsurf_mcp",
    "resolve_memory_server",
]
