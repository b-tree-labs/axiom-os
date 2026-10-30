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
from axiom.infra.paths import get_project_state_dir, get_user_state_dir
from axiom.llm.local_models import QUICK as _QUICK_MODEL  # ADR-140


def _get_global_settings_path() -> Path:
    return get_user_state_dir() / "settings.toml"


_PROJECT_SETTINGS_PATH = get_project_state_dir(_REPO_ROOT) / "settings.toml"

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
    "routing.ollama_model": _QUICK_MODEL,
    "routing.audit_log": True,
    # Sensitivity governs how aggressively the router flags content as
    # export-controlled. "balanced" is the right default for most deployments
    # (research, teaching, internal tooling). Operators with a hard EC
    # obligation explicitly opt into "strict". "permissive" is for offline
    # / cost-sensitive paths where every keyword match still triggers EC
    # but Ollama isn't consulted.
    "routing.sensitivity": "balanced",
    # Chat advisor: the post-turn hook that OFFERS one dim tip (never executes).
    # `chat.advisor.slm` opts a session into asking the LOCAL small model
    # (the router's Ollama path) for a next step; ships OFF so no session
    # makes a model call it did not ask for.
    "chat.advisor.enabled": True,
    "chat.advisor.slm": False,
    # Interface
    "interface.stream": True,
    "interface.theme": "dark",
    # RACI trust level (1-5): 1=locked_down, 2=cautious, 3=balanced, 4=autonomous, 5=full_trust
    "raci.trust": 2,
    # Review preset for the shadow risk tier. autonomous is trust 4-5 and
    # is the shipped default. The tier is computed and logged; the lander
    # does not merge or hold from it. autonomy.enabled still ships off.
    # Opt out with `settings set raci.preset strict`.
    "raci.preset": "autonomous",
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
    # State snapshots (axiom.infra.state_snapshots; `axi state`). Retention is
    # GFS-style: everything for 24h, then 1/day x14, 1/week x8, 1/month x6, the
    # newest 24 regardless of age, hard caps of 100 snapshots / 2 GiB, and the
    # newest 3 + any pinned + the newest verified are never pruned. Reasoning:
    # spec-state-snapshots.md.
    "state.snapshots.enabled": True,
    "state.snapshots.interval_hours": 6,
    "state.snapshots.auto_prune": True,
    "state.snapshots.keep_last": 24,
    "state.snapshots.keep_recent_hours": 24,
    "state.snapshots.keep_daily": 14,
    "state.snapshots.keep_weekly": 8,
    "state.snapshots.keep_monthly": 6,
    "state.snapshots.protect_newest": 3,
    "state.snapshots.max_count": 100,
    "state.snapshots.max_bytes": 2 * 1024**3,
    "state.snapshots.min_free_bytes": 1024**3,
    "state.snapshots.max_file_bytes": 64 * 1024 * 1024,
    "state.snapshots.exclude": [],
    # Vault backup (axiom.infra.vault_backup; `axi vault backup`). Encrypted copies of the credential
    # VALUES the harness holds, which state snapshots never copy. Daily; a change schedules one after a
    # 5 minute debounce; retention is shorter-dense / longer-monthly than snapshots. Reasoning:
    # spec-vault-backup.md.
    "vault.backup.enabled": True,
    "vault.backup.interval_hours": 24,
    "vault.backup.debounce_seconds": 300,
    "vault.backup.auto_prune": True,
    "vault.backup.second_copy": "",
    "vault.backup.extra_files": [],
    "vault.backup.keep_last": 10,
    "vault.backup.keep_recent_hours": 6,
    "vault.backup.keep_daily": 14,
    "vault.backup.keep_weekly": 8,
    "vault.backup.keep_monthly": 24,
    "vault.backup.protect_newest": 3,
    "vault.backup.max_count": 60,
    "vault.backup.max_bytes": 256 * 1024**2,
}


def _load_toml(path: Path) -> dict[str, Any]:
    """Lenient read for callers that only want values: damage reads as empty."""
    data, _problem = _load_toml_checked(path)
    return data


def _settings_fix(path: Path) -> str:
    return (
        f"repair {path} by hand (the unreadable content is still there), restore a good copy, "
        f"or start from defaults with `mv '{path}' '{path}.bad'`"
    )


def _load_toml_checked(path: Path) -> tuple[dict[str, Any], str | None]:
    """Read a settings file. Returns ``(data, problem)``; ``problem`` is None when sound.

    A missing file is not a problem (defaults apply). A file that exists but is
    unreadable, not TOML, or the wrong shape is reported, never silently read
    as empty: settings carry the trust dial, the autonomy switch and the export
    control sensitivity, and "empty" means the permissive defaults.
    """
    import tomllib

    from axiom.infra.state_integrity import StateDamaged, read_state_text

    try:
        text = read_state_text(path, fix=_settings_fix(path))
    except StateDamaged as exc:
        return {}, str(exc)
    if text is None:
        return {}, None
    if not text.strip():
        if text == "":
            return (
                {},
                f"{path} is empty (zero bytes), which is what a cut-short write leaves. Fix: {_settings_fix(path)}",
            )
        return {}, None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return {}, f"{path} is not valid TOML ({exc}). Fix: {_settings_fix(path)}"
    for key, value in data.items():
        if not isinstance(value, dict) and any(d.startswith(key + ".") for d in _DEFAULTS):
            return {}, (
                f"{path} has the wrong shape: [{key}] must be a table, found "
                f"{type(value).__name__}. Fix: {_settings_fix(path)}"
            )
    return data, None


#: What a setting reads as while its file is damaged. The most restrictive value
#: of each dial that gates what the platform may do on its own. Damage must not
#: be a way to a looser posture than the operator last chose.
_SAFE_ON_DAMAGE: dict[str, Any] = {
    "raci.trust": 1,
    "raci.preset": "strict",
    "autonomy.enabled": False,
    "routing.sensitivity": "strict",
}


def _save_toml(path: Path, data: dict[str, Any]) -> None:
    from axiom.infra.state_integrity import atomic_write_text

    try:
        import tomli_w  # type: ignore[import]
    except ImportError:
        atomic_write_text(path, "\n".join(_dict_to_toml(data)) + "\n")
        return
    atomic_write_text(path, tomli_w.dumps(data))


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


def coerce(key: str, raw: Any) -> Any:
    """A typed value from what a person (or a transport) wrote.

    The CLI's rules, in one place so every surface parses identically:
    true/yes and false/no are booleans, ``[a, b]`` is a list, a key whose
    default is a list takes a comma-separated list, an integer is an
    integer, anything else is the text. A non-string passes through.
    """
    if not isinstance(raw, str):
        return raw
    if raw.lower() in ("true", "yes"):
        return True
    if raw.lower() in ("false", "no"):
        return False
    if raw.startswith("[") and raw.endswith("]"):
        return [v.strip().strip("'\"") for v in raw[1:-1].split(",") if v.strip()]
    if isinstance(_DEFAULTS.get(key), list):
        return [v.strip() for v in raw.split(",") if v.strip()]
    try:
        return int(raw)
    except ValueError:
        return raw


class SettingsStore:
    """Merged view of global + project settings with read/write support."""

    def __init__(self) -> None:
        import logging

        log = logging.getLogger(__name__)
        self._problems: dict[str, str] = {}
        gpath = _get_global_settings_path()
        gdata, gproblem = _load_toml_checked(gpath)
        ppath = _PROJECT_SETTINGS_PATH
        pdata, pproblem = _load_toml_checked(ppath)
        self._global = _flatten(gdata)
        self._project = _flatten(pdata)
        for scope, problem in (("global", gproblem), ("project", pproblem)):
            if problem:
                self._problems[scope] = problem
                log.error(
                    "settings are damaged, so the restrictive values %s apply until it is "
                    "fixed: %s",
                    sorted(_SAFE_ON_DAMAGE),
                    problem,
                )
        # Applied in get()/all(), never merged into _global: _global is what gets
        # written back, and a safe override must not be persisted as a choice.
        self._safe: dict[str, Any] = dict(_SAFE_ON_DAMAGE) if self._problems else {}

    def _refuse_if_damaged(self, scope: str) -> None:
        if scope in self._problems:
            from axiom.infra.state_integrity import StateDamaged

            path = _get_global_settings_path() if scope == "global" else _PROJECT_SETTINGS_PATH
            raise StateDamaged(path, "is damaged, so it was not overwritten", _settings_fix(path))

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._project:
            return self._project[key]
        if key in self._global:
            return self._global[key]
        if key in self._safe:
            return self._safe[key]
        return _DEFAULTS.get(key, default)

    def all(self) -> dict[str, Any]:
        merged = dict(_DEFAULTS)
        merged.update(self._safe)
        merged.update(self._global)
        merged.update(self._project)
        return merged

    def set(self, key: str, value: Any, scope: str = "project") -> None:
        self._refuse_if_damaged("global" if scope == "global" else "project")
        if scope == "global":
            self._global[key] = value
            _save_toml(_get_global_settings_path(), _unflatten(self._global))
        else:
            self._project[key] = value
            _save_toml(_PROJECT_SETTINGS_PATH, _unflatten(self._project))

    def reset(self, key: str, scope: str = "project") -> bool:
        self._refuse_if_damaged("global" if scope != "project" else "project")
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
    # The site's agent policy (ADR-164 node file) outranks this switch: a node
    # whose site declared no agents runs none, whatever autonomy says.
    try:
        from axiom.infra import node_functions as _nf

        if _nf.load().agent_policy != "local":
            return False
    except Exception:  # noqa: BLE001 - an unreadable node file reads as a general install
        pass
    val = SettingsStore().get("autonomy.enabled", False)
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes", "on"}
    return bool(val)
