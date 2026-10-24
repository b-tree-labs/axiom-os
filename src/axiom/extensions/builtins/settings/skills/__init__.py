# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``settings.list`` / ``settings.get`` / ``settings.set`` — settings on every surface (ADR-056).

Until these existed settings had a CLI and nothing else: no MCP tool, no HTTP
route, so the web Settings panel and an agent could not read what the node was
configured to do.

Two keys are SAFETY SWITCHES — ``autonomy.enabled`` (the off-switch beneath
autonomy) and ``raci.trust`` (how eagerly actions auto-approve). They are
readable everywhere and settable ONLY from the node's own CLI: setting
``autonomy.enabled`` has OS-level side effects the CLI performs, and an agent
or a browser session raising its own autonomy is exactly what the switch
exists to prevent. ``settings.set`` refuses them and names the command.

Values that may carry a credential (a database URL, a token) are never
returned by these skills: their value is replaced by ``"(set)"`` or ``""``.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec, default_registry

#: Settable only at the node's own CLI. See the module docstring.
SAFETY_SWITCHES = frozenset({"autonomy.enabled", "raci.trust"})

#: A key whose name suggests a credential has its value withheld.
_SECRETISH = ("url", "token", "secret", "password", "key", "dsn", "credential")


def is_secretish(key: str) -> bool:
    leaf = key.rsplit(".", 1)[-1].lower()
    return any(word in leaf for word in _SECRETISH)


def _shown(key: str, value: Any) -> Any:
    if is_secretish(key):
        return "(set)" if value not in ("", None) else ""
    return value


def _cli() -> str:
    from axiom.infra.branding import get_branding

    return get_branding().cli_name


def _store():
    from ..store import SettingsStore

    return SettingsStore()


def _defaults() -> dict[str, Any]:
    from ..store import _DEFAULTS

    return dict(_DEFAULTS)


def _row(key: str, value: Any, defaults: dict[str, Any]) -> dict[str, Any]:
    default = defaults.get(key)
    return {
        "key": key,
        "value": _shown(key, value),
        "default": _shown(key, default),
        "changed": key in defaults and value != default,
        "safety_switch": key in SAFETY_SWITCHES,
    }


def list_settings(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    prefix = str(params.get("prefix") or "").strip()
    defaults = _defaults()
    rows = [
        _row(k, v, defaults)
        for k, v in sorted(_store().all().items())
        if not prefix or k.startswith(prefix)
    ]
    return SkillResult(value={"settings": rows})


def get_setting(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    key = str(params.get("key") or "").strip()
    if not key:
        return SkillResult(ok=False, errors=["name the setting: key is required"])
    store, defaults = _store(), _defaults()
    value = store.get(key)
    if value is None and key not in defaults:
        return SkillResult(ok=False, errors=[f"no setting {key!r}; `{_cli()} settings` lists them"])
    return SkillResult(value=_row(key, value, defaults))


def set_setting(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from ..store import coerce

    key = str(params.get("key") or "").strip()
    if not key:
        return SkillResult(ok=False, errors=["name the setting: key is required"])
    if key in SAFETY_SWITCHES:
        return SkillResult(
            ok=False,
            errors=[
                f"{key} is a safety switch and is changed only at this node's own CLI: "
                f"`{_cli()} settings set {key} <value>`"
            ],
        )
    if "value" not in params:
        return SkillResult(ok=False, errors=["give the new value: value is required"])
    scope = str(params.get("scope") or "global")
    if scope not in ("global", "project"):
        return SkillResult(ok=False, errors=["scope is 'global' or 'project'"])
    value = coerce(key, params["value"])
    _store().set(key, value, scope=scope)
    return SkillResult(value=_row(key, _store().get(key), _defaults()) | {"scope": scope})


def bind(registry: SkillRegistry) -> list[str]:
    specs = [
        (
            "settings.list",
            list_settings,
            "List this node's settings with their defaults and whether each was changed. "
            "Credential-like values are withheld. Read-only.",
            {"prefix": "optional key prefix, e.g. 'routing.'"},
            False,
            {"risk": "read"},
        ),
        (
            "settings.get",
            get_setting,
            "Read one setting, with its default. Credential-like values are withheld. Read-only.",
            {"key": "the dotted setting name (required)"},
            False,
            {"risk": "read"},
        ),
        (
            "settings.set",
            set_setting,
            "Change one setting. Refuses the safety switches (autonomy.enabled, raci.trust), "
            "which change only at the node's own CLI.",
            {
                "key": "the dotted setting name (required)",
                "value": "the new value; true/false, [a, b] and integers are typed",
                "scope": "'global' (default) or 'project'",
            },
            True,
            # Reversible (set it back, or `settings reset`); nothing has to hold
            # first — the safety switches are refused in the function itself.
            {"risk": "reversible-write", "requires": ()},
        ),
    ]
    names = []
    for name, fn, description, inputs, mutating, chaining in specs:
        if not registry.has(name):
            registry.register_skill(
                SkillSpec(
                    name=name,
                    fn=fn,
                    description=description,
                    inputs=inputs,
                    side_effects=mutating,
                    idempotent=True,
                    surfaces=("cli", "mcp", "agent_tool", "skill_md"),
                    **chaining,
                ),
                mutating=mutating,
            )
        names.append(name)
    return names


register = bind


def bind_default() -> SkillRegistry:
    registry = default_registry()
    bind(registry)
    return registry


__all__ = ["SAFETY_SWITCHES", "bind", "bind_default", "is_secretish", "register"]
