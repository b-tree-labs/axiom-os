# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Register the axiom-memory MCP server in each LLM tool's user-scope config.

Per-tool registrar protocol — each tool (Claude Code, Codex, Gemini,
OpenCode, …) has different config formats and locations. Rather than
bake per-tool branches into one big function, each tool gets a registrar
declaring three callables: ``detect`` (is it installed?), ``register``
(write the entry idempotently), ``is_registered`` (read-only check).

`axi memory register-mcp --all` walks the registry, calling ``register``
on every tool that ``detect`` reports True for.

Public API:

- :data:`TOOL_REGISTRARS` — the registry: ``{tool_name: ToolRegistrar}``.
- :func:`detect_installed_tools` — returns ``{tool_name: bool}``.
- :func:`register_axiom_memory_mcp` — Claude Code (kept for back-compat).
- :func:`is_axiom_memory_mcp_registered` — Claude Code (back-compat).
- :func:`register_codex_mcp` / :func:`is_codex_mcp_registered`.
- Stubs for gemini, opencode raise ``NotImplementedError`` with a
  contributor pointer to this file.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# The MCP entry-point is stable across worktrees + install modes.
_MCP_ARGS: list[str] = ["-m", "axiom.extensions.builtins.memory.mcp_server"]
_SERVER_KEY = "axiom-memory"


# ===========================================================================
# Claude Code registrar (~/.claude.json)
# ===========================================================================


def _default_claude_config_path() -> Path:
    """User-scope claude config path, with ``$AXIOM_CLAUDE_CONFIG`` override."""
    override = os.environ.get("AXIOM_CLAUDE_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".claude.json"


def _load_json_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text() or "{}")
    except json.JSONDecodeError:
        raise


def _save_json_config(config_path: Path, data: dict[str, Any]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(data, indent=2) + "\n")


def _expected_claude_entry(python_path: str) -> dict[str, Any]:
    return {
        "type": "stdio",
        "command": python_path,
        "args": list(_MCP_ARGS),
        "env": {},
    }


def register_axiom_memory_mcp(
    *,
    config_path: Path | None = None,
    python_path: str | None = None,
) -> dict[str, Any]:
    """Write or update the axiom-memory user-scope registration in Claude Code config.

    Idempotent — same shape as before (back-compat preserved). Returns
    ``{"action": "added"|"updated"|"unchanged", "command": ..., "config_path": ...}``.
    """
    config_path = config_path or _default_claude_config_path()
    python_path = python_path or sys.executable

    data = _load_json_config(config_path)
    mcp_servers = data.setdefault("mcpServers", {})
    expected = _expected_claude_entry(python_path)
    existing = mcp_servers.get(_SERVER_KEY)

    if existing == expected:
        return {
            "action": "unchanged",
            "command": python_path,
            "config_path": str(config_path),
            "tool": "claude-code",
        }

    action = "updated" if existing is not None else "added"
    mcp_servers[_SERVER_KEY] = expected
    _save_json_config(config_path, data)
    return {
        "action": action,
        "command": python_path,
        "config_path": str(config_path),
        "tool": "claude-code",
    }


def is_axiom_memory_mcp_registered(
    *,
    config_path: Path | None = None,
    expected_command: str | None = None,
) -> dict[str, Any]:
    """Detect Claude Code axiom-memory registration status."""
    config_path = config_path or _default_claude_config_path()

    if not config_path.exists():
        return {
            "registered": False,
            "reason": "missing",
            "config_path": str(config_path),
            "tool": "claude-code",
        }

    try:
        data = _load_json_config(config_path)
    except json.JSONDecodeError as exc:
        return {
            "registered": False,
            "reason": "malformed_config",
            "detail": str(exc),
            "config_path": str(config_path),
            "tool": "claude-code",
        }

    mcp_servers = data.get("mcpServers") or {}
    entry = mcp_servers.get(_SERVER_KEY)
    if entry is None:
        return {
            "registered": False,
            "reason": "missing",
            "config_path": str(config_path),
            "tool": "claude-code",
        }

    command = entry.get("command", "")
    stale = False
    if expected_command is not None and command != expected_command:
        try:
            stale = os.path.realpath(command) != os.path.realpath(expected_command)
        except OSError:
            stale = True
    return {
        "registered": True,
        "command": command,
        "stale": bool(stale),
        "config_path": str(config_path),
        "tool": "claude-code",
    }


# ===========================================================================
# Codex registrar (~/.codex/config.toml)
# ===========================================================================


def _default_codex_config_path() -> Path:
    """User-scope codex config path, with ``$AXIOM_CODEX_CONFIG`` override."""
    override = os.environ.get("AXIOM_CODEX_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".codex" / "config.toml"


def _load_toml_config(config_path: Path):
    """Load TOML, preserving comments + structure via tomlkit. Returns a
    tomlkit Document (dict-like) so subsequent edits round-trip cleanly.
    """
    import tomlkit
    if not config_path.exists() or not config_path.read_text().strip():
        return tomlkit.document()
    return tomlkit.loads(config_path.read_text())


def _save_toml_config(config_path: Path, data) -> None:
    import tomlkit
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(tomlkit.dumps(data))


def _expected_codex_entry(python_path: str) -> dict[str, Any]:
    # Note: codex's `mcp_servers` schema doesn't carry a `type` field the way
    # ~/.claude.json does — codex only supports stdio mcp servers.
    return {
        "command": python_path,
        "args": list(_MCP_ARGS),
    }


def register_codex_mcp(
    *,
    config_path: Path | None = None,
    python_path: str | None = None,
) -> dict[str, Any]:
    """Write or update the axiom-memory entry in Codex user-scope config."""
    config_path = config_path or _default_codex_config_path()
    python_path = python_path or sys.executable

    data = _load_toml_config(config_path)
    mcp_servers = data.setdefault("mcp_servers", {})
    expected = _expected_codex_entry(python_path)
    existing = mcp_servers.get(_SERVER_KEY)

    if existing == expected:
        return {
            "action": "unchanged",
            "command": python_path,
            "config_path": str(config_path),
            "tool": "codex",
        }

    action = "updated" if existing is not None else "added"
    mcp_servers[_SERVER_KEY] = expected
    _save_toml_config(config_path, data)
    return {
        "action": action,
        "command": python_path,
        "config_path": str(config_path),
        "tool": "codex",
    }


def is_codex_mcp_registered(
    *,
    config_path: Path | None = None,
    expected_command: str | None = None,
) -> dict[str, Any]:
    """Detect Codex axiom-memory registration status."""
    config_path = config_path or _default_codex_config_path()

    if not config_path.exists():
        return {
            "registered": False,
            "reason": "missing",
            "config_path": str(config_path),
            "tool": "codex",
        }

    try:
        data = _load_toml_config(config_path)
    except Exception as exc:
        return {
            "registered": False,
            "reason": "malformed_config",
            "detail": str(exc),
            "config_path": str(config_path),
            "tool": "codex",
        }

    mcp_servers = data.get("mcp_servers") or {}
    entry = mcp_servers.get(_SERVER_KEY)
    if entry is None:
        return {
            "registered": False,
            "reason": "missing",
            "config_path": str(config_path),
            "tool": "codex",
        }

    command = entry.get("command", "")
    stale = False
    if expected_command is not None and command != expected_command:
        try:
            stale = os.path.realpath(command) != os.path.realpath(expected_command)
        except OSError:
            stale = True
    return {
        "registered": True,
        "command": command,
        "stale": bool(stale),
        "config_path": str(config_path),
        "tool": "codex",
    }


# ===========================================================================
# Stub registrars (Gemini, OpenCode) — surface contributor pointers
# ===========================================================================


def _stub_register(tool_name: str):
    def _register(**_kwargs) -> dict[str, Any]:
        raise NotImplementedError(
            f"register_{tool_name.replace('-', '_')}_mcp is not yet implemented. "
            f"To add it, follow the codex registrar pattern in "
            f"axiom/extensions/builtins/memory/register_mcp.py: "
            f"add register_{tool_name}_mcp + is_{tool_name}_mcp_registered + "
            f"a TOOL_REGISTRARS entry. Each tool's config format and location "
            f"differs — see {tool_name}'s docs for its MCP config path/schema."
        )
    return _register


def _stub_is_registered(tool_name: str):
    def _check(**_kwargs) -> dict[str, Any]:
        return {
            "registered": False,
            "reason": "registrar_not_implemented",
            "tool": tool_name,
        }
    return _check


# ===========================================================================
# Detection — does the tool appear installed on this machine?
# ===========================================================================


def _detect_claude_code() -> bool:
    """Detect Claude Code by checking for ~/.claude/ dir or `claude` on PATH."""
    return Path.home().joinpath(".claude").exists() or shutil.which("claude") is not None


def _detect_codex() -> bool:
    """Detect Codex by checking for `codex` on PATH or ~/.codex/."""
    return shutil.which("codex") is not None or Path.home().joinpath(".codex").exists()


def _detect_gemini() -> bool:
    """Detect Gemini CLI by checking for `gemini` on PATH or ~/.gemini/."""
    return shutil.which("gemini") is not None or Path.home().joinpath(".gemini").exists()


def _detect_opencode() -> bool:
    """Detect OpenCode by checking for `opencode` on PATH or ~/.opencode/."""
    return shutil.which("opencode") is not None or Path.home().joinpath(".opencode").exists()


def detect_installed_tools() -> dict[str, bool]:
    """Return ``{tool_name: detected_bool}`` for every tool in the registry."""
    return {name: reg.detect() for name, reg in TOOL_REGISTRARS.items()}


# ===========================================================================
# Registry
# ===========================================================================


@dataclass(frozen=True)
class ToolRegistrar:
    """Per-tool registrar bundling detect + register + is_registered."""

    name: str
    detect: Callable[[], bool]
    register: Callable[..., dict[str, Any]]
    is_registered: Callable[..., dict[str, Any]]


TOOL_REGISTRARS: dict[str, ToolRegistrar] = {
    "claude-code": ToolRegistrar(
        name="claude-code",
        detect=_detect_claude_code,
        register=register_axiom_memory_mcp,
        is_registered=is_axiom_memory_mcp_registered,
    ),
    "codex": ToolRegistrar(
        name="codex",
        detect=_detect_codex,
        register=register_codex_mcp,
        is_registered=is_codex_mcp_registered,
    ),
    "gemini": ToolRegistrar(
        name="gemini",
        detect=_detect_gemini,
        register=_stub_register("gemini"),
        is_registered=_stub_is_registered("gemini"),
    ),
    "opencode": ToolRegistrar(
        name="opencode",
        detect=_detect_opencode,
        register=_stub_register("opencode"),
        is_registered=_stub_is_registered("opencode"),
    ),
}


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
    reported as ``{"action": "skipped", "reason": "not_detected"}``.
    Tools whose registrar raises NotImplementedError are reported as
    ``{"action": "stub", "reason": str(exc)}`` so the caller sees the
    contributor pointer without the whole sweep failing.
    """
    python_path = python_path or sys.executable
    detected = detected if detected is not None else detect_installed_tools()
    results: dict[str, dict[str, Any]] = {}
    for name, reg in TOOL_REGISTRARS.items():
        if not detected.get(name):
            results[name] = {"action": "skipped", "reason": "not_detected", "tool": name}
            continue
        try:
            results[name] = reg.register(python_path=python_path)
        except NotImplementedError as exc:
            results[name] = {"action": "stub", "reason": str(exc), "tool": name}
    return results
