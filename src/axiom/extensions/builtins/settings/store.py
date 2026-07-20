# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Settings store — reads and writes axi settings.toml at global and project scope.

Two scopes (mirrors Claude Code's .claude/ pattern):
  global   → ~/<state-dir>/settings.toml    user-wide preferences
  project  → .neut/settings.toml            repo-local overrides (gitignored)

Deliberately separate from runtime/config/ which is owned by `axi config`
(the facility setup wizard: API keys, facility config, model config).

  axi config   → runtime/config/     facility setup (admin, one-time onboarding)
  axi settings → .neut/ + ~/<state-dir>/   user preferences (runtime, per-user)

Project settings take precedence over global.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.paths import get_user_state_dir


def _get_global_settings_path() -> Path:
    return get_user_state_dir() / "settings.toml"


_PROJECT_SETTINGS_PATH = _REPO_ROOT / ".neut" / "settings.toml"

_DEFAULTS: dict[str, Any] = {
    # User identity (captured during axi config)
    "user.name": "",
    "user.email": "",
    "user.org": "",  # e.g., "example.org"
    "user.org_tenant": "",  # Microsoft tenant for org SSO (e.g., "example.onmicrosoft.com")
    # Routing
    "routing.default_mode": "auto",
    "routing.cloud_provider": "anthropic",
    "routing.prefer_provider": [],
    "routing.prefer_when": "reachable",
    "routing.on_vpn_unavailable": "warn",
    "routing.ollama_model": "llama3.2:1b",
    "routing.audit_log": True,
    # Sensitivity governs how aggressively the router flags content as
    # export-controlled. "balanced" is the right default for most deployments
    # (research, teaching, internal tooling). Operators with a hard EC
    # obligation explicitly opt into "strict". "permissive" is for offline
    # / cost-sensitive paths where every keyword match still triggers EC
    # but Ollama isn't consulted.
    "routing.sensitivity": "balanced",
    # Interface
    "interface.stream": True,
    "interface.theme": "dark",
    # RACI trust level (1-5): 1=locked_down, 2=cautious, 3=balanced, 4=autonomous, 5=full_trust
    "raci.trust": 2,
    # Master autonomy switch. Ships OFF: a fresh install runs NO heartbeat /
    # background agents until an operator opts in (`axi settings set
    # autonomy.enabled true`, or the onboarding interview). This is the "big
    # off switch" beneath the graduated autonomy dial — see the autonomy-dial
    # ADR. `raci.trust` (above) governs how eagerly actions auto-approve
    # *within* what autonomy unlocks; this governs whether anything runs
    # autonomously at all.
    "autonomy.enabled": False,
    # Publisher
    "publisher.cooldown_seconds": 300,  # 5 min debounce — skip republish during active editing
    # RAG
    "rag.database_url": "",  # empty = RAG disabled; set to postgresql:// to enable
    # Memory — pinned default principal_id used by axi memory CLI + MCP server
    # when caller omits an explicit principal. Eliminates the cross-identity
    # footgun (e.g. UT email vs Anthropic-account email). See
    # `feedback_axi_memory_principal.md` for canonical-principal guidance.
    "memory.default_principal": "",
}


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    from axiom.infra.toml_compat import load_toml

    return load_toml(path)


def _save_toml(path: Path, data: dict[str, Any]) -> None:
    try:
        import tomli_w  # type: ignore[import]
    except ImportError:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = _dict_to_toml(data)
        path.write_text("\n".join(lines) + "\n")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(data))


def _dict_to_toml(data: dict[str, Any], prefix: str = "") -> list[str]:
    """Minimal TOML serializer for nested string/bool/int dicts."""
    lines: list[str] = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    nested = {k: v for k, v in data.items() if isinstance(v, dict)}

    if prefix:
        lines.append(f"\n[{prefix}]")
    for k, v in scalars.items():
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, str):
            lines.append(f'{k} = "{v}"')
        else:
            lines.append(f"{k} = {v}")
    for k, v in nested.items():
        section = f"{prefix}.{k}" if prefix else k
        lines += _dict_to_toml(v, prefix=section)
    return lines


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for k, v in data.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            result.update(_flatten(v, prefix=key))
        else:
            result[key] = v
    return result


def _unflatten(flat: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for dotted_key, value in flat.items():
        parts = dotted_key.split(".")
        d = result
        for part in parts[:-1]:
            d = d.setdefault(part, {})
        d[parts[-1]] = value
    return result


class SettingsStore:
    """Merged view of global + project settings with read/write support."""

    def __init__(self) -> None:
        self._global = _flatten(_load_toml(_get_global_settings_path()))
        self._project = _flatten(_load_toml(_PROJECT_SETTINGS_PATH))

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._project:
            return self._project[key]
        if key in self._global:
            return self._global[key]
        return _DEFAULTS.get(key, default)

    def all(self) -> dict[str, Any]:
        merged = dict(_DEFAULTS)
        merged.update(self._global)
        merged.update(self._project)
        return merged

    def set(self, key: str, value: Any, scope: str = "project") -> None:
        if scope == "global":
            self._global[key] = value
            _save_toml(_get_global_settings_path(), _unflatten(self._global))
        else:
            self._project[key] = value
            _save_toml(_PROJECT_SETTINGS_PATH, _unflatten(self._project))

    def reset(self, key: str, scope: str = "project") -> bool:
        target = self._project if scope == "project" else self._global
        path = _PROJECT_SETTINGS_PATH if scope == "project" else _get_global_settings_path()
        if key in target:
            del target[key]
            _save_toml(path, _unflatten(target))
            return True
        return False


def autonomy_enabled() -> bool:
    """Master autonomy gate for heartbeat / background agents.

    Ships OFF: ``autonomy.enabled`` defaults to ``False`` (see ``_DEFAULTS``)
    so a fresh install runs NO autonomous heartbeats until an operator opts in
    — via ``axi settings set autonomy.enabled true`` or the onboarding
    interview. Checked at both the install choke point
    (``register_all_daemon_agents``) and the runtime one
    (``background_service_main``). See the autonomy-dial ADR.

    Accepts a bool or a truthy string (``"true"``/``"on"``/``"1"``/``"yes"``)
    since a hand-edited settings.toml may carry either.
    """
    val = SettingsStore().get("autonomy.enabled", False)
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes", "on"}
    return bool(val)
