# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One-command installer for the MCP server.

Registers the MCP server — the Axiom aggregation server by default, or a
consumer-provided entrypoint via ``AXIOM_MCP_SERVER_MODULE`` — into every
detected MCP-capable IDE / TUI, idempotently, so a user never hand-edits a
config file.

Most MCP clients share one JSON shape — ``{"mcpServers": {name: {command,
args, env}}}`` — so a single writer covers the majority; VS Code uses a
``servers`` key with a per-entry ``type``; Codex uses TOML (``mcp_servers``).
Clients are declared in :data:`TOOL_SPECS`; adding another is one row.

Idempotent: re-running reports ``unchanged`` and writes nothing when the entry
already matches. Every default path has an ``AXIOM_<TOOL>_CONFIG`` env override
(used by tests and non-default homes).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DEFAULT_SERVER_MODULE = "axiom.extensions.builtins.mcp.server"
_DEFAULT_SERVER_NAME = "axiom"


def resolve_server() -> tuple[str, str, list[str]]:
    """Return ``(server_name, command, args)`` for the MCP server to register.

    Defaults to the Axiom aggregation server. A consumer layer can override
    the entrypoint (e.g. to register a branded re-export that discovers its
    own extensions) via ``AXIOM_MCP_SERVER_MODULE`` (and optionally
    ``AXIOM_MCP_SERVER_NAME``) — keeping consumer identity in the consumer,
    not baked into the platform.
    """
    command = sys.executable
    module = os.environ.get("AXIOM_MCP_SERVER_MODULE") or _DEFAULT_SERVER_MODULE
    name = os.environ.get("AXIOM_MCP_SERVER_NAME") or (
        _DEFAULT_SERVER_NAME
        if module == _DEFAULT_SERVER_MODULE
        else module.split(".")[0].replace("_", "-")
    )
    return name, command, ["-m", module]


def server_env() -> dict[str, str]:
    """Env block written into each IDE entry."""
    env: dict[str, str] = {}
    try:
        from axiom.infra.paths import get_project_root

        env["AXIOM_ROOT"] = str(get_project_root())
    except Exception:
        pass
    db = os.environ.get("DATABASE_URL")
    if db:
        env["DATABASE_URL"] = db
    return env


# Clients whose model-endpoint wiring is actually IMPLEMENTED today. A client
# is only stamped EC-capable if its endpoint was genuinely pointed in-enclave —
# stamping EC-capable for a client we don't actually wire (so its model is still
# a public cloud) would falsely disable the EC withhold gate. Add a client here
# only once its wire_* function exists and runs under --route-model.
ENDPOINT_WIRED_CLIENTS: frozenset[str] = frozenset({"claude-code", "vscode"})


def _ec_capable(spec: ToolSpec, route_model: bool) -> bool:
    """A client is EC-capable only when its model was actually put in-enclave:
    model_routable AND routed (--route-model) AND its endpoint wiring is
    implemented. Otherwise its model is still a public cloud and the MCP server
    must withhold EC tool output (so we stamp it non-EC, fail-closed)."""
    return bool(
        spec.model_routable
        and route_model
        and spec.name in ENDPOINT_WIRED_CLIENTS
    )


def _client_env(spec: ToolSpec, *, route_model: bool) -> dict[str, str]:
    """Per-client MCP env block: the shared server env plus the client's
    identity and EC-capability, which the server reads to gate EC tool output
    (see mcp.routing.gate_result_for_client)."""
    env = server_env()
    env["AXIOM_MCP_CLIENT"] = spec.name
    env["AXIOM_MCP_CLIENT_EC_CAPABLE"] = "true" if _ec_capable(spec, route_model) else "false"
    return env


# ---------------------------------------------------------------------------
# Platform config roots
# ---------------------------------------------------------------------------


def _config_home() -> Path:
    """Per-OS config root: macOS Application Support, Windows %APPDATA%,
    else XDG (~/.config)."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


# ---------------------------------------------------------------------------
# Client specs — one row per supported IDE/TUI
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fmt: str  # "mcp_json" | "vscode_json" | "codex_toml"
    env_override: str  # env var that overrides the config path
    default_path: Callable[[], Path]
    detect: Callable[[], bool]
    include_type: bool = False  # mcp_json: write "type":"stdio" (Claude does)
    # --- EC capability matrix (single source of truth) ---
    # model_routable: can this client's MODEL be pointed at the local Axiom
    #   ingress (so inference stays in-enclave)? False for clients that proxy
    #   inference through their own cloud and expose no installable endpoint
    #   config (e.g. Cursor).
    # A client is EC-capable only when model_routable AND it was actually
    #   routed (--route-model). Otherwise its model is a public cloud and it
    #   is an exfiltration sink — the MCP server withholds EC tool output.
    # Fail-closed: clients not yet verified are model_routable=False.
    model_routable: bool = True
    protocol: str = "openai"  # "anthropic" | "openai" | "other" — endpoint shape
    ec_notes: str = ""  # short human note for the capability chart


def _h(*parts: str) -> Path:
    return Path.home().joinpath(*parts)


def _present(*, dirs: tuple[str, ...] = (), bins: tuple[str, ...] = ()) -> Callable[[], bool]:
    def _check() -> bool:
        if any(_h(d).exists() for d in dirs):
            return True
        return any(shutil.which(b) is not None for b in bins)

    return _check


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "claude-code", "mcp_json", "AXIOM_CLAUDE_CONFIG",
        lambda: _h(".claude.json"),
        _present(dirs=(".claude", ".claude.json"), bins=("claude",)),
        include_type=True,
        protocol="anthropic",
        ec_notes="Routed via ANTHROPIC_BASE_URL+AUTH_TOKEN; direct connection. Verified.",
    ),
    ToolSpec(
        "claude-desktop", "mcp_json", "AXIOM_CLAUDE_DESKTOP_CONFIG",
        lambda: _config_home() / "Claude" / "claude_desktop_config.json",
        lambda: (_config_home() / "Claude").exists(),
        protocol="anthropic",
        ec_notes="Anthropic-protocol; routable via base-URL config (same engine as claude-code).",
    ),
    ToolSpec(
        "cursor", "mcp_json", "AXIOM_CURSOR_CONFIG",
        lambda: _h(".cursor", "mcp.json"),
        _present(dirs=(".cursor",), bins=("cursor",)),
        # Cursor proxies inference through its own cloud and exposes no
        # installable endpoint config — its model cannot be put in-enclave, so
        # it is never EC-capable (MCP tools only). See docs/specs.
        model_routable=False,
        ec_notes="BLOCKED: proxies inference through Cursor cloud; no installable endpoint config. Never EC-capable.",
    ),
    ToolSpec(
        "windsurf", "mcp_json", "AXIOM_WINDSURF_CONFIG",
        lambda: _h(".codeium", "windsurf", "mcp_config.json"),
        _present(dirs=(".codeium/windsurf",), bins=("windsurf",)),
        model_routable=False,  # fail-closed until verified
        ec_notes="UNVERIFIED: Codeium-hosted; routing/data-path not confirmed. Treated non-EC pending verification.",
    ),
    ToolSpec(
        "gemini", "mcp_json", "AXIOM_GEMINI_CONFIG",
        lambda: _h(".gemini", "settings.json"),
        _present(dirs=(".gemini",), bins=("gemini",)),
        model_routable=False,  # fail-closed until verified
        ec_notes="UNVERIFIED: Gemini CLI is Google-hosted; custom-endpoint routing not confirmed. Treated non-EC pending verification.",
    ),
    ToolSpec(
        "opencode", "mcp_json", "AXIOM_OPENCODE_CONFIG",
        lambda: _config_home() / "opencode" / "opencode.json",
        _present(dirs=(".opencode",), bins=("opencode",)),
        ec_notes="OpenAI-compatible base-URL config; direct connection.",
    ),
    ToolSpec(
        "vscode", "vscode_json", "AXIOM_VSCODE_CONFIG",
        lambda: _config_home() / "Code" / "User" / "mcp.json",
        _present(bins=("code",)),
        ec_notes=(
            "Copilot BYOK (VS Code >=1.122, direct, no sign-in) or Continue; EC-capable when routed. "
            "Caveat: Tab completions + embeddings still run on GitHub infra — disable for EC."
        ),
    ),
    ToolSpec(
        "codex", "codex_toml", "AXIOM_CODEX_CONFIG",
        lambda: _h(".codex", "config.toml"),
        _present(dirs=(".codex",), bins=("codex",)),
        ec_notes="OpenAI Codex CLI; base_url in config.toml; direct connection.",
    ),
)

_SPEC_BY_NAME = {s.name: s for s in TOOL_SPECS}


def _config_path(spec: ToolSpec) -> Path:
    return Path(os.environ.get(spec.env_override) or spec.default_path())


def detect_tools() -> dict[str, bool]:
    """``{tool: present?}`` across all supported clients."""
    return {s.name: s.detect() for s in TOOL_SPECS}


# ---------------------------------------------------------------------------
# Writers (idempotent). action ∈ added|updated|unchanged|would-add|would-update
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    if path.exists() and path.read_text().strip():
        return json.loads(path.read_text())
    return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _install_json(
    path: Path, key: str, server_name: str, entry: dict[str, Any], *, dry_run: bool
) -> str:
    data = _load_json(path)
    servers = data.setdefault(key, {})
    existing = servers.get(server_name)
    if existing == entry:
        return "unchanged"
    is_new = existing is None
    if dry_run:
        return "would-add" if is_new else "would-update"
    servers[server_name] = entry
    _write_json(path, data)
    return "added" if is_new else "updated"


def _install_codex(
    path: Path, server_name: str, command: str, args: list[str], env: dict[str, str], *, dry_run: bool
) -> str:
    import tomlkit

    doc = tomlkit.loads(path.read_text()) if (path.exists() and path.read_text().strip()) else tomlkit.document()
    servers = doc.get("mcp_servers")
    if servers is None:
        servers = tomlkit.table()
        doc["mcp_servers"] = servers
    expected: dict[str, Any] = {"command": command, "args": list(args)}
    if env:
        expected["env"] = dict(env)
    existing = servers.get(server_name)
    if existing is not None and {k: dict(v) if k == "env" else (list(v) if isinstance(v, list) else v) for k, v in dict(existing).items()} == expected:
        return "unchanged"
    is_new = existing is None
    if dry_run:
        return "would-add" if is_new else "would-update"
    entry = tomlkit.table()
    entry["command"] = command
    entry["args"] = list(args)
    if env:
        entry["env"] = dict(env)
    servers[server_name] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc))
    return "added" if is_new else "updated"


def _install_one(spec: ToolSpec, server_name: str, command: str, args: list[str], env: dict[str, str], *, dry_run: bool) -> str:
    path = _config_path(spec)
    if spec.fmt == "mcp_json":
        entry: dict[str, Any] = {"command": command, "args": list(args), "env": env}
        if spec.include_type:
            entry = {"type": "stdio", **entry}
        return _install_json(path, "mcpServers", server_name, entry, dry_run=dry_run)
    if spec.fmt == "vscode_json":
        entry = {"type": "stdio", "command": command, "args": list(args), "env": env}
        return _install_json(path, "servers", server_name, entry, dry_run=dry_run)
    if spec.fmt == "codex_toml":
        return _install_codex(path, server_name, command, args, env, dry_run=dry_run)
    return "unsupported"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def supported_tools() -> list[str]:
    return [s.name for s in TOOL_SPECS]


def client_capabilities() -> list[dict[str, Any]]:
    """The agent-harness × EC-routing capability matrix (single source of truth).

    One row per supported client. ``ec_routable`` answers the headline
    question — *is it OK to route this harness to an export-controlled model?*
    A harness is EC-routable iff its model can be put in-enclave
    (``model_routable``); the MCP server withholds EC tool output from any
    client that is not. Consumed by ``axi mcp clients`` and the
    ``axiom_mcp__client_capabilities`` MCP tool.
    """
    rows: list[dict[str, Any]] = []
    for s in TOOL_SPECS:
        rows.append({
            "client": s.name,
            "protocol": s.protocol,
            "mcp_tools": True,  # every supported client gets the MCP tool surface
            "ec_routable": s.model_routable,
            "ec_status": "capable" if s.model_routable else "blocked",
            "notes": s.ec_notes,
        })
    return rows


# ---------------------------------------------------------------------------
# Ingress service (one managed OS unit) + per-client model base-URL wiring
# ---------------------------------------------------------------------------

_INGRESS_PORT = int(os.environ.get("AXIOM_INGRESS_PORT", "8788"))
_INGRESS_URL = f"http://127.0.0.1:{_INGRESS_PORT}"


def ensure_ingress_service(*, dry_run: bool = False) -> dict[str, str]:
    """Ensure the LLM ingress runs as the one managed service. Idempotent —
    a no-op (``running``) when it's already up; otherwise install + start."""
    try:
        from axiom.infra.services import ServiceManager, ServiceStatus
    except Exception as exc:  # noqa: BLE001
        return {"action": "error", "detail": f"services unavailable: {exc}"}

    svc_env: dict[str, str] = {}
    try:
        from axiom.infra.paths import get_project_root

        svc_env["AXIOM_ROOT"] = str(get_project_root())
    except Exception:
        pass
    tier = os.environ.get("AXIOM_BRIDGE_ROUTING_TIER")
    if tier:
        svc_env["AXIOM_BRIDGE_ROUTING_TIER"] = tier

    mgr = ServiceManager(
        name="gateway-ingress",
        binary=sys.executable,
        args=["-m", "axiom.llm.anthropic_ingress", "--port", str(_INGRESS_PORT)],
        env=svc_env,
    )
    if mgr.status().status == ServiceStatus.RUNNING:
        return {"action": "running", "service": mgr._svc.service_id, "provider": mgr.provider_name}
    if dry_run:
        return {"action": "would-start", "service": mgr._svc.service_id, "provider": mgr.provider_name}
    mgr.install()
    mgr.start()
    return {"action": "started", "service": mgr._svc.service_id, "provider": mgr.provider_name}


def _claude_settings_path() -> Path:
    return Path(os.environ.get("AXIOM_CLAUDE_SETTINGS") or Path.home() / ".claude" / "settings.json")


def wire_claude_base_url(url: str = _INGRESS_URL, *, dry_run: bool = False) -> dict[str, str]:
    """Point Claude Code's model at the ingress via settings.json ``env``.
    Idempotent — ``unchanged`` when already set to this URL."""
    path = _claude_settings_path()
    data: dict[str, Any] = {}
    if path.exists() and path.read_text().strip():
        data = json.loads(path.read_text())
    env = data.setdefault("env", {})
    if env.get("ANTHROPIC_BASE_URL") == url:
        return {"action": "unchanged", "config_path": str(path)}
    is_new = "ANTHROPIC_BASE_URL" not in env
    if dry_run:
        return {"action": "would-add" if is_new else "would-update", "config_path": str(path)}
    env["ANTHROPIC_BASE_URL"] = url
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return {"action": "added" if is_new else "updated", "config_path": str(path)}


def _vscode_chatmodels_path() -> Path:
    return Path(
        os.environ.get("AXIOM_VSCODE_CHATMODELS")
        or _config_home() / "Code" / "User" / "chatLanguageModels.json"
    )


_VSCODE_PROVIDER_NAME = "Axiom Gateway"


def wire_vscode_byok(url: str = _INGRESS_URL, *, model_id: str = "axiom-gateway",
                     dry_run: bool = False) -> dict[str, str]:
    """Point VS Code Copilot (BYOK) at the ingress via chatLanguageModels.json.

    Writes a ``customendpoint`` provider whose model URL is the local ingress's
    OpenAI chat-completions path. Idempotent. The ``apiKey`` is a placeholder
    (the ingress ignores auth); whether Copilot accepts it without a GUI key
    prompt is verified empirically — if it prompts, that is the one residual
    manual step, surfaced to the user.

    Top-level shape is an ARRAY of providers (VS Code >= 1.109 BYOK schema).
    """
    path = _vscode_chatmodels_path()
    chat_url = url.rstrip("/") + "/v1/chat/completions"
    provider = {
        "name": _VSCODE_PROVIDER_NAME,
        "vendor": "customendpoint",
        "apiKey": "axiom-local",
        "apiType": "chat-completions",
        "models": [{
            "id": model_id,
            "name": "Axiom (in-enclave)",
            "url": chat_url,
            "toolCalling": True,
            "vision": False,
            "maxInputTokens": 128000,
            "maxOutputTokens": 16000,
        }],
    }
    data: list[Any] = []
    if path.exists() and path.read_text().strip():
        loaded = json.loads(path.read_text())
        if isinstance(loaded, list):
            data = loaded
    # Replace any existing Axiom provider; preserve the user's others.
    others = [p for p in data if not (isinstance(p, dict) and p.get("name") == _VSCODE_PROVIDER_NAME)]
    existing = next((p for p in data if isinstance(p, dict) and p.get("name") == _VSCODE_PROVIDER_NAME), None)
    if existing == provider:
        return {"action": "unchanged", "config_path": str(path)}
    is_new = existing is None
    if dry_run:
        return {"action": "would-add" if is_new else "would-update", "config_path": str(path)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([*others, provider], indent=2) + "\n")
    return {"action": "added" if is_new else "updated", "config_path": str(path)}


def install(
    *,
    tools: list[str] | None = None,
    dry_run: bool = False,
    all_tools: bool = False,
    route_model: bool = False,
) -> dict[str, Any]:
    """Register the unified MCP server into requested (or detected) clients.

    ``tools`` None → detected clients only, unless ``all_tools`` (write every
    supported client's config regardless of detection).

    MCP registration is always additive/safe. ``route_model`` (opt-in) is the
    invasive part: it starts the LLM ingress service and **redirects each
    client's model** at it (e.g. Claude Code's ANTHROPIC_BASE_URL). Off by
    default so running ``axi mcp install`` never silently repoints the IDE a
    developer is using — they ask for it explicitly (``--route-model``).
    """
    server_name, command, args = resolve_server()
    env = server_env()
    detected = detect_tools()

    if tools:
        targets = tools
    elif all_tools:
        targets = supported_tools()
    else:
        targets = [t for t, present in detected.items() if present]

    results: dict[str, dict[str, str]] = {}
    for tool in targets:
        spec = _SPEC_BY_NAME.get(tool)
        if spec is None:
            results[tool] = {"action": "unknown-tool", "config_path": ""}
            continue
        path = _config_path(spec)
        client_env = _client_env(spec, route_model=route_model)
        results[tool] = {
            "action": _install_one(spec, server_name, command, args, client_env, dry_run=dry_run),
            "config_path": str(path),
            "ec_capable": "true" if _ec_capable(spec, route_model) else "false",
        }

    # Model redirect is OPT-IN (route_model). Default install is MCP-only:
    # additive, safe to run in the IDE you're developing with.
    ingress: dict[str, str] = {"action": "skipped"}
    if route_model:
        # One managed service (one OS unit) for the LLM ingress. Idempotent.
        ingress = ensure_ingress_service(dry_run=dry_run)
        # Point each Anthropic-protocol client's model at the ingress.
        if "claude-code" in targets:
            results["claude-code"]["base_url"] = wire_claude_base_url(dry_run=dry_run)["action"]
        # VS Code Copilot BYOK -> ingress (OpenAI chat-completions).
        if "vscode" in targets:
            results["vscode"]["chat_models"] = wire_vscode_byok(dry_run=dry_run)["action"]

    return {
        "server": server_name,
        "command": command,
        "args": args,
        "env": env,
        "dry_run": dry_run,
        "route_model": route_model,
        "detected": detected,
        "ingress": ingress,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Uninstall — turn the server off for a given client (or all)
# ---------------------------------------------------------------------------

# Server names this installer may have written. Uninstall removes the
# default plus the currently-resolved name (which honors any consumer
# override via AXIOM_MCP_SERVER_NAME), so "turn it off" works for both.
def _our_server_names() -> tuple[str, ...]:
    return tuple({_DEFAULT_SERVER_NAME, resolve_server()[0]})


def _uninstall_json(path: Path, key: str, *, dry_run: bool) -> str:
    if not path.exists() or not path.read_text().strip():
        return "absent"
    data = _load_json(path)
    servers = data.get(key) or {}
    present = [n for n in _our_server_names() if n in servers]
    if not present:
        return "absent"
    if dry_run:
        return "would-remove"
    for n in present:
        del servers[n]
    _write_json(path, data)
    return "removed"


def _uninstall_codex(path: Path, *, dry_run: bool) -> str:
    if not path.exists() or not path.read_text().strip():
        return "absent"
    import tomlkit

    doc = tomlkit.loads(path.read_text())
    servers = doc.get("mcp_servers") or {}
    present = [n for n in _our_server_names() if n in servers]
    if not present:
        return "absent"
    if dry_run:
        return "would-remove"
    for n in present:
        del servers[n]
    path.write_text(tomlkit.dumps(doc))
    return "removed"


def _uninstall_one(spec: ToolSpec, *, dry_run: bool) -> str:
    path = _config_path(spec)
    if spec.fmt == "mcp_json":
        return _uninstall_json(path, "mcpServers", dry_run=dry_run)
    if spec.fmt == "vscode_json":
        return _uninstall_json(path, "servers", dry_run=dry_run)
    if spec.fmt == "codex_toml":
        return _uninstall_codex(path, dry_run=dry_run)
    return "unsupported"


def uninstall(
    *, tools: list[str] | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """Remove the MCP server entry from a client (or every supported client).

    Default target is *all* supported clients (removing our entry wherever it
    exists; ``absent`` is a no-op), so ``axi mcp uninstall`` turns it off
    everywhere. Use ``tools`` (``--tool``) to scope to one IDE. Only removes
    our own server entries; other MCP servers in the config are untouched.
    """
    targets = tools or supported_tools()
    results: dict[str, dict[str, str]] = {}
    for tool in targets:
        spec = _SPEC_BY_NAME.get(tool)
        if spec is None:
            results[tool] = {"action": "unknown-tool", "config_path": ""}
            continue
        results[tool] = {
            "action": _uninstall_one(spec, dry_run=dry_run),
            "config_path": str(_config_path(spec)),
        }
    return {"dry_run": dry_run, "results": results}


__all__ = [
    "TOOL_SPECS",
    "detect_tools",
    "install",
    "resolve_server",
    "server_env",
    "supported_tools",
    "uninstall",
]
