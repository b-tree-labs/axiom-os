# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The node switches: list, enable, disable, role and the site policies (ADR-164, ADR-254).

Every write here goes through the same audit the flag verbs use: the skill is
mutating, so ``invoke_capability`` journals it, and each one notes its change
(``note_change``) so the change log shows old and new. A switch of the
``extension`` or ``function`` kind is also recorded in the switchboard's table
when this node has a database (``features.record``); the node file stays the
copy discovery reads.
"""

from __future__ import annotations

from typing import Any

from axiom.infra import node_functions as nf
from axiom.infra.audit_trail import DEFAULT_PRINCIPAL, note_change
from axiom.infra.skills import SkillContext, SkillResult

from .. import record
from ..registry import KINDS, SCOPES_BY_KIND, Switch, classify, function_rows, switchboard
from ._flags import CLEAR, flag_metadata, this_node


def _cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name or "axi"
    except Exception:  # noqa: BLE001
        return "axi"


def _commands(*, include_off: bool = False) -> dict[str, dict]:
    try:
        from axiom.axiom_cli import SUBCOMMANDS
        from axiom.extensions.discovery import discover_cli_commands

        return {
            noun: info
            for noun, info in discover_cli_commands(include_off=include_off).items()
            if noun not in SUBCOMMANDS
        }
    except Exception:  # noqa: BLE001
        return {}


def _optional(cfg: nf.NodeConfig | None = None) -> dict[str, dict]:
    """Optional extensions (off until a node turns them on), and whether each is on."""
    try:
        from axiom.infra.optional_extensions import optional_extensions

        return optional_extensions(cfg=cfg)
    except Exception:  # noqa: BLE001
        return {}


def _service(cfg: nf.NodeConfig | None = None):
    """The flag resolver; a node whose role needs no database reads no override store."""
    from ..resolver import FlagService, NoOverrides

    cfg = cfg if cfg is not None else nf.load()
    if not nf.needs_database(cfg):
        return FlagService(overrides=NoOverrides())
    return FlagService()


def _actor(ctx: SkillContext | None) -> str:
    return getattr(ctx, "actor", None) or DEFAULT_PRINCIPAL


def snapshot(
    cfg: nf.NodeConfig,
    *,
    commands: dict[str, dict] | None = None,
    kind: str | None = None,
    prefix: str = "",
    user: str | None = None,
    account: str | None = None,
    include_flags: bool = False,
) -> dict[str, Any]:
    """What this node runs, grouped by nothing; the caller groups.

    Feature flags are counted (``flags``: how many, and the verb that lists them) and
    left out of ``rows``: ``flags.list`` lists them. ``include_flags`` keeps their rows,
    for a caller that reads the whole registry.
    """
    commands = _commands(include_off=True) if commands is None else commands
    optional = _optional(cfg)
    svc = _service(cfg)
    try:
        resolution = svc.resolve(node=this_node(), account=account, user=user)
        meta, overrides = flag_metadata(svc)
    except Exception as exc:  # noqa: BLE001 - no flag store is not a reason to hide the node switches
        from ..resolver import Resolution

        resolution = Resolution(environment="", flags={}, sources={})
        meta, overrides = {}, {}
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        problem = f"flag overrides could not be read, so flag rows are not shown ({first_line})"
    else:
        problem = ""
    rows = switchboard(
        cfg,
        commands=commands,
        optional=optional,
        resolution=resolution,
        metadata=meta,
        overrides=overrides,
        kind=kind,
        prefix=prefix,
    )
    flag_count = len(meta)
    if not include_flags:
        rows = [r for r in rows if r.kind != "flag"]
        problem = problem.replace("so flag rows are not shown", "so flags are not counted")
    problems = [p for p in (cfg.problem, problem) if p]
    drifted = record.drift_items(cfg, rows)
    record.alert_drift(drifted)  # one alert per drift, once; report-only, never reconciled
    return {
        "role": cfg.role,
        "functions": list(cfg.functions),
        "features": list(cfg.features),
        "confined": cfg.confined,
        "agent_policy": cfg.agent_policy,
        "maintenance": cfg.maintenance_level,
        "record_of_truth": cfg.record_of_truth_in_force,
        "config": str(nf.config_path()),
        "problem": "; ".join(problems),
        "environment": resolution.environment,
        "kinds": list(KINDS),
        "scopes": {k: list(v) for k, v in SCOPES_BY_KIND.items()},
        "rows": [r.as_dict() for r in rows],
        "drift": [d.line for d in drifted],
        "flags": flags_pointer(flag_count),
        "roles": {
            name: {"functions": list(r.functions), "description": r.description}
            for name, r in nf.roles().items()
        },
    }


def _snapshot(cfg: nf.NodeConfig, commands: dict[str, dict] | None = None) -> dict[str, Any]:
    return snapshot(cfg, commands=commands)


def flags_pointer(count: int) -> dict[str, Any]:
    """Where the feature flags are listed: ``features`` points there and never lists them."""
    return {"count": count, "skill": "flags.list", "command": f"{_cli()} flags list"}


def list_(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``features.list``: what this node runs. Flags are counted and pointed at, never listed."""
    kind = str(params.get("kind") or "").strip() or None
    if kind is not None and kind not in KINDS:
        return SkillResult(ok=False, errors=[f"kind is one of {', '.join(KINDS)}, not {kind!r}"])
    # kind "flag" lists no rows: the pointer in ``flags`` says where they are.
    return SkillResult(value=snapshot(nf.load(), kind=kind))


def _a_flag(name: str, value: str = "on") -> SkillResult:
    return SkillResult(
        ok=False,
        errors=[
            f"{name!r} is a feature flag, and `{_cli()} features` covers what this node runs: set it with "
            f"`{_cli()} flags set {name} {value}` (MCP axiom_flags__set)"
        ],
    )


# ------------------------------------------------------------------ the node-scope switch


def _kind_of(name: str, *, cfg: nf.NodeConfig, prefer: str) -> tuple[str | None, dict, dict, dict]:
    commands = _commands()
    optional = _optional(cfg)
    try:
        flags = _service(cfg).get_registry()
    except Exception:  # noqa: BLE001
        flags = {}
    return classify(name, commands=commands, optional=optional, flags=flags, prefer=prefer), commands, optional, flags


def _owned_by_off_extension(name: str, cfg: nf.NodeConfig) -> str | None:
    """The off extension that provides command ``name``, if that is why it is unknown."""
    everything = _commands(include_off=True)
    info = everything.get(name)
    if not info:
        return None
    owner = str(info.get("extension") or "")
    if owner and not _optional(cfg).get(owner, {}).get("enabled", True):
        return owner
    return None


def _unknown(name: str, cfg: nf.NodeConfig) -> SkillResult:
    owner = _owned_by_off_extension(name, cfg)
    if owner:
        return SkillResult(
            ok=False,
            errors=[
                f"{name!r} is provided by the extension {owner!r}, which is off on this node; "
                f"turn it on first with `{_cli()} features enable {owner}`"
            ],
        )
    return SkillResult(
        ok=False,
        errors=[f"nothing on this node is called {name!r}; `{_cli()} features list` names what this node runs"],
    )


def _state_of(name: str, kind: str, cfg: nf.NodeConfig, commands: dict, optional: dict) -> str:
    if kind == "extension":
        return "on" if optional.get(name, {}).get("enabled") else "off"
    off = frozenset(n for n, info in optional.items() if not info.get("enabled"))
    rows = {r.name: r for r in function_rows(cfg, commands, off)}
    row = rows.get(name)
    return "on" if row is not None and row.enabled else "off"


def _switch_node(
    name: str, value: str, *, ctx: SkillContext, cfg: nf.NodeConfig, kind: str, commands: dict, optional: dict
) -> tuple[nf.NodeConfig, str]:
    """Write one node-scope switch of kind extension or function: the node file, then the record."""
    before = _state_of(name, kind, cfg, commands, optional)
    if value == "on":
        known = {**{n: {} for n in optional}, **commands}
        new_cfg = nf.enable(name, known=known)
    elif value == "off":
        new_cfg = nf.disable(name, optional=kind == "extension")
    else:  # clear
        new_cfg = nf.forget(name)
    after = _state_of(name, kind, new_cfg, commands, _optional(new_cfg))
    note_change(kind=kind, subject=name, old=before, new=after)
    recorded = record.mirror(kind, name, None if value == CLEAR else after, by=_actor(ctx), cfg=new_cfg)
    return new_cfg, recorded


def enable(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = str(params.get("name") or "").strip()
    if not name:
        return SkillResult(ok=False, errors=["name a function, command, extension or background-agents to turn on"])
    cfg = nf.load()
    kind, commands, optional, _flags = _kind_of(name, cfg=cfg, prefer="switch")
    if kind is None:
        return _unknown(name, cfg)
    if kind == "flag":
        return _a_flag(name, "on")
    try:
        cfg, recorded = _switch_node(name, "on", ctx=ctx, cfg=cfg, kind=kind, commands=commands, optional=optional)
    except (nf.UnknownFeature, nf.AgentPolicyRefused) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(value=snapshot(cfg), actions_taken=[f"turned on {name} ({kind}; record {recorded})"])


def disable(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = str(params.get("name") or "").strip()
    if not name:
        return SkillResult(ok=False, errors=["name what to turn off"])
    cfg = nf.load()
    kind, commands, optional, _flags = _kind_of(name, cfg=cfg, prefer="switch")
    if kind is None:
        return _unknown(name, cfg)
    if kind == "flag":
        return _a_flag(name, "off")
    try:
        cfg, recorded = _switch_node(name, "off", ctx=ctx, cfg=cfg, kind=kind, commands=commands, optional=optional)
    except nf.RoleFunction as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(value=snapshot(cfg), actions_taken=[f"turned off {name} ({kind}; record {recorded})"])


def set_switch(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``features.set``: an extension or function at node scope, ``on``, ``off`` or ``clear``.

    A feature flag is not set here: ``flags.set`` is its verb (owner decision 2026-11-12).
    """
    key = str(params.get("key") or "").strip()
    if not key:
        return SkillResult(ok=False, errors=["name the extension or function: key is required"])
    raw = params.get("value")
    if raw in (None, ""):
        return SkillResult(ok=False, errors=[f"give the value: on, off or {CLEAR}"])
    value = str(raw).strip().lower()
    cfg = nf.load()
    wanted = str(params.get("kind") or "").strip() or None
    if wanted == "flag":
        return _a_flag(key, value)
    if wanted is not None and wanted not in KINDS:
        return SkillResult(ok=False, errors=[f"kind is extension or function, not {wanted!r}"])
    kind, commands, optional, _flags = _kind_of(key, cfg=cfg, prefer="switch")
    if kind is None:
        return _unknown(key, cfg)
    if kind == "flag":
        return _a_flag(key, value)
    scope = str(params.get("scope") or "node").strip().lower()
    scope_id = str(params.get("scope_id") or "").strip()
    if scope not in SCOPES_BY_KIND[kind]:
        return SkillResult(
            ok=False,
            errors=[
                f"{key!r} is a{'n' if kind == 'extension' else ''} {kind}: it changes what runs on a "
                f"machine, so it is set at node scope only (`--scope node`), never for an account or a user"
            ],
        )
    if scope_id and scope_id != this_node():
        return SkillResult(
            ok=False,
            errors=[f"a{'n' if kind == 'extension' else ''} {kind} is set on the node itself; this node is {this_node()!r}, not {scope_id!r}"],
        )
    if value not in ("on", "off", CLEAR):
        return SkillResult(
            ok=False,
            errors=[f"{raw!r} is not on, off or {CLEAR}; `soon` is a feature-flag value, and {key!r} is a{'n' if kind == 'extension' else ''} {kind}"],
        )
    try:
        cfg, recorded = _switch_node(key, value, ctx=ctx, cfg=cfg, kind=kind, commands=commands, optional=optional)
    except (nf.UnknownFeature, nf.AgentPolicyRefused, nf.RoleFunction) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    after = _state_of(key, kind, cfg, commands, _optional(cfg))
    return SkillResult(
        value={
            "key": key,
            "kind": kind,
            "scope": "node",
            "scope_id": this_node(),
            "value": after,
            "cleared": value == CLEAR,
            "record": recorded,
            "updated_by": _actor(ctx),
        },
        actions_taken=[
            f"cleared {key} ({kind}); it is now {after} by default"
            if value == CLEAR
            else f"set {key}={after} ({kind}, node {this_node()}; record {recorded})"
        ],
    )


# ------------------------------------------------------------------ role and the site policies


def role(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Apply a named role, or ``clear`` to show everything again."""
    name = str(params.get("name") or "").strip()
    settings = dict(params.get("settings") or {})
    before = nf.load().role
    if name in ("clear", "none", ""):
        cfg = nf.clear()
        note_change(kind="function", subject="role", old=before or None, new=None)
        return SkillResult(value=snapshot(cfg), actions_taken=["cleared the role; every command shows"])
    known = nf.roles()
    if name not in known:
        choices = ", ".join(sorted(known)) or "none are installed"
        return SkillResult(ok=False, errors=[f"no role called {name!r}; roles: {choices}"])
    spec = known[name]
    merged = {**dict(spec.settings), **settings}
    cfg = nf.apply(role=name, functions=spec.functions, settings=merged)
    note_change(kind="function", subject="role", old=before or None, new=name)
    return SkillResult(
        value=snapshot(cfg),
        actions_taken=[f"this node's role is now {name} ({', '.join(spec.functions)})"],
    )


def agents(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Declare the site's agent policy: none, assist or local."""
    policy = str(params.get("policy") or "").strip()
    before = nf.load().agent_policy
    try:
        cfg = nf.set_agent_policy(policy)
    except nf.UnknownFeature as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    note_change(kind="function", subject="agents", old=before, new=cfg.agent_policy)
    return SkillResult(value=snapshot(cfg), actions_taken=[f"this site's agent policy is now {policy}"])


def maintenance(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Declare how much remote maintenance this site accepts: off, requests or sessions."""
    level = str(params.get("level") or "").strip()
    before = nf.load().maintenance_level
    try:
        cfg = nf.set_maintenance_level(level)
    except nf.UnknownFeature as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    note_change(kind="function", subject="maintenance", old=before, new=cfg.maintenance_level)
    return SkillResult(value=snapshot(cfg), actions_taken=[f"this site's remote maintenance is now {level}"])


def record_of_truth(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Declare which copy of this site's data wins: upstream or local (ADR-180)."""
    truth = str(params.get("truth") or "").strip()
    before = nf.load().record_of_truth_in_force
    try:
        cfg = nf.set_record_of_truth(truth)
    except nf.UnknownFeature as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    note_change(kind="function", subject="record-of-truth", old=before, new=cfg.record_of_truth_in_force)
    meaning = (
        "the host holds this site's shared record (contributor)"
        if truth == "upstream"
        else "this node is the record; the host holds a copy of what is shared (local-first)"
    )
    return SkillResult(
        value=snapshot(cfg), actions_taken=[f"record of truth is now {truth}: {meaning}"]
    )


__all__ = [
    "Switch",
    "_snapshot",
    "agents",
    "disable",
    "enable",
    "flags_pointer",
    "list_",
    "maintenance",
    "record_of_truth",
    "role",
    "set_switch",
    "snapshot",
]
