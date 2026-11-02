# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/skills``: the platform's skills (capabilities), and whether the chat agent can call each one now.

Read-only. Nothing here keeps a list of its own:

* the catalog is the ``SkillRegistry`` (the chat process's own registry, plus the skills every discovered
  extension binds, so a skill the chat registry has not loaded is still listed and says so);
* "can the agent call it now" is answered from ``get_all_tools()``, the table the chat turn really offers
  the model, with the reason read from the three gates a capability passes: it has a ``SkillSpec``, it
  declared the ``agent_tool`` surface (``exposed_on``), and its namespace is in ``chat.tool_namespaces``;
* names, schema and the read/write decision come from ``capability_projection``, the shared projector.

``list_skills`` and ``describe_skill`` are pure functions returning dataclasses, so an MCP tool or an
agent can reuse them. Nothing here is registered as an MCP tool.
"""

from __future__ import annotations

import importlib
import json
import logging
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from axiom.infra.capability_projection import (
    capability_to_surface_name,
    exposed_on,
    inputs_to_json_schema,
    split_shape,
)
from axiom.infra.skills import SkillRegistry

from ..command_registry import CliSpec, CommandContext, McpForm, command
from ..follow_ups import FollowUp, pick, remember_list, resolve_ref, result, with_json_follow_ups

log = logging.getLogger(__name__)

G_AGENT = "Agent"
NAMESPACES_SETTING = "chat.tool_namespaces"
AGENT_SURFACE = "agent_tool"

#: Why a skill is not callable by the model right now.
REASON_NO_SPEC = "no SkillSpec: not discoverable by the model"
REASON_NOT_AGENT_FACING = "has not declared the agent_tool surface"
REASON_NOT_IN_CHAT_REGISTRY = "not loaded in this chat process's skill registry"
REASON_NAMESPACE_OFF = "namespace not enabled"
REASON_WITHHELD = "namespace enabled but the chat loop withholds it right now"


def _brand_cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001 - a label never takes the command down
        return "axi"


# ---- data ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillRow:
    name: str  # press.draft
    namespace: str
    description: str
    has_spec: bool
    agent_facing: bool  # declared the agent_tool surface
    namespace_enabled: bool  # in chat.tool_namespaces
    in_chat_registry: bool
    callable_now: bool  # the tool is in the table the model is offered
    reason: str  # why not, empty when callable_now
    enable_command: str | None  # the exact setting command that would enable it, when that is the fix
    tool_name: str  # the chat/LLM surface name, press__draft
    mutating: bool | None  # None = undeclared (treated as mutating)
    surfaces: tuple[str, ...]
    inputs: dict[str, str] = field(default_factory=dict, hash=False)
    cli_verb: str | None = None
    mcp_tool: str | None = None
    mcp_exposed: bool = False


@dataclass(frozen=True)
class SkillsReport:
    rows: tuple[SkillRow, ...]
    missing: tuple[str, ...]  # registered, but with no SkillSpec
    total: int  # every registered skill, specced or not
    enabled_namespaces: tuple[str, ...]
    source: str  # where the catalog came from

    @property
    def callable_count(self) -> int:
        return sum(1 for r in self.rows if r.callable_now)


# ---- the catalog --------------------------------------------------------------------------------

_CATALOG_CACHE: list[SkillRegistry] = []


def discover_catalog(*, refresh: bool = False) -> SkillRegistry:
    """A registry holding the skills every discovered extension binds.

    A fresh registry, never the process-global one: listing must not change what the chat can call. Binding
    imports each extension's ``skills`` package, which is slow, so the result is kept for the process.
    A package that fails to import is skipped and logged; one broken extension does not hide the rest.
    """
    if _CATALOG_CACHE and not refresh:
        return _CATALOG_CACHE[0]
    from axiom.extensions.discovery import _extension_source_package, surfaced_extensions

    registry = SkillRegistry()
    for ext in surfaced_extensions():
        root_pkg = _extension_source_package(ext)
        if not root_pkg or not getattr(ext, "name", None):
            continue
        try:
            mod = importlib.import_module(f"{root_pkg}.extensions.builtins.{ext.name}.skills")
            binder = getattr(mod, "bind", None)
            if callable(binder):
                binder(registry)
        except Exception as exc:  # noqa: BLE001
            log.debug("skills catalog: %s did not bind: %s", ext.name, exc)
    _CATALOG_CACHE[:] = [registry]
    return registry


def _chat_registry() -> SkillRegistry | None:
    from .. import tools as chat_tools

    try:
        return chat_tools._skill_registry()  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        log.debug("skills: chat registry unavailable: %s", exc)
        return None


def _merged(catalog: SkillRegistry, chat: SkillRegistry | None) -> SkillRegistry:
    """Everything in ``catalog``, plus anything only the chat registry holds. Neither input is changed."""
    merged = SkillRegistry()
    for src in (chat, catalog):
        if src is None:
            continue
        for name in src.list():
            merged.adopt(src, name)
    return merged


def _offered_tool_names() -> set[str] | None:
    """The names of the tools the model is offered this turn, or None when that table cannot be built."""
    from ..tools import get_all_tools

    try:
        return set(get_all_tools())
    except Exception as exc:  # noqa: BLE001
        log.debug("skills: tool table unavailable: %s", exc)
        return None


def _enabled_namespaces() -> list[str]:
    from .. import tools as chat_tools

    return list(chat_tools._skill_namespaces())  # noqa: SLF001


def _mcp_name(capability: str) -> str | None:
    try:
        from axiom.extensions.builtins.mcp.skill_tools import mcp_tool_name

        return mcp_tool_name(capability)
    except Exception:  # noqa: BLE001 - the mcp extra may not be installed; unknown, not guessed
        return None


def _enable_command(namespace: str, enabled: list[str]) -> str:
    wanted = [*enabled, namespace] if namespace not in enabled else list(enabled)
    return f"{_brand_cli()} settings set {NAMESPACES_SETTING} {','.join(wanted)}"


def _row(
    name: str,
    spec,
    *,
    enabled: list[str],
    chat_names: set[str],
    offered: set[str] | None,
) -> SkillRow:
    ns = name.split(".", 1)[0]
    tool = capability_to_surface_name(name)
    facing = exposed_on(spec, AGENT_SURFACE)
    ns_on = ns in enabled
    in_chat = name in chat_names
    if offered is not None:
        callable_now = tool in offered
    else:  # the tool table could not be built: fall back to the three gates themselves
        callable_now = facing and ns_on and in_chat
    reason, fix = "", None
    if not callable_now:
        if not facing:
            reason = REASON_NOT_AGENT_FACING
        elif not in_chat:
            reason = REASON_NOT_IN_CHAT_REGISTRY
            fix = None if ns_on else _enable_command(ns, enabled)
        elif not ns_on:
            reason, fix = REASON_NAMESPACE_OFF, _enable_command(ns, enabled)
        else:
            reason = REASON_WITHHELD
    surfaces = tuple(str(s) for s in (spec.surfaces or ()))
    mcp_on = "mcp" in surfaces
    # Names come from the shared projector, not from here. By convention (infra/skills.py) the namespace
    # is the providing extension's CLI noun, so the verb is `<cli> <namespace> <verb>`.
    verb = name.split(".", 1)[1] if "." in name else name
    return SkillRow(
        name=name,
        namespace=ns,
        description=(spec.description or "").strip(),
        has_spec=True,
        agent_facing=facing,
        namespace_enabled=ns_on,
        in_chat_registry=in_chat,
        callable_now=callable_now,
        reason=reason,
        enable_command=fix,
        tool_name=tool,
        mutating=None if spec.side_effects is None else bool(spec.side_effects),
        surfaces=surfaces,
        inputs=dict(spec.inputs or {}),
        cli_verb=f"{_brand_cli()} {ns} {verb}" if "cli" in surfaces else None,
        mcp_tool=_mcp_name(name) if mcp_on else None,
        mcp_exposed=mcp_on,
    )


def list_skills(
    *,
    catalog: SkillRegistry | None = None,
    chat_registry: SkillRegistry | None = None,
    namespaces: list[str] | None = None,
    offered: set[str] | None = None,
    needle: str = "",
) -> SkillsReport:
    """Every registered skill with the answer to "can the chat agent call it now".

    Arguments exist so a caller (or a test) can supply the registries; omitted, the real ones are read.
    ``offered=None`` means "ask ``get_all_tools()``"; pass a set to skip that scan.
    """
    chat = chat_registry if chat_registry is not None else _chat_registry()
    base = catalog if catalog is not None else discover_catalog()
    registry = _merged(base, chat)
    enabled = list(namespaces) if namespaces is not None else _enabled_namespaces()
    chat_names = set(chat.list()) if chat is not None else set()
    if offered is None:
        offered = _offered_tool_names()
    specs = registry.specs()
    n = needle.strip().lower()

    def _match(name: str, desc: str) -> bool:
        return not n or n in name.lower() or n in desc.lower()

    rows = tuple(
        _row(name, spec, enabled=enabled, chat_names=chat_names, offered=offered)
        for name, spec in sorted(specs.items())
        if _match(name, spec.description or "")
    )
    missing = tuple(sorted(nm for nm in registry.list() if nm not in specs and _match(nm, "")))
    return SkillsReport(
        rows=rows,
        missing=missing,
        total=len(registry.list()),
        enabled_namespaces=tuple(enabled),
        source="chat skill registry + skills bound by discovered extensions",
    )


def filter_report(report: SkillsReport, needle: str) -> SkillsReport:
    """Rows and missing names whose name, namespace or description contains ``needle``."""
    n = needle.strip().lower()
    if not n:
        return report
    rows = tuple(
        r for r in report.rows if n in r.name.lower() or n in r.description.lower()
    )
    missing = tuple(m for m in report.missing if n in m.lower())
    return replace(report, rows=rows, missing=missing)


def describe_skill(name: str, **kwargs: Any) -> SkillRow | None:
    """The row for exactly ``name``, or None."""
    for row in list_skills(**kwargs).rows:
        if row.name == name:
            return row
    return None


# ---- rendering ----------------------------------------------------------------------------------


def _first_line(text: str) -> str:
    text = (text or "").strip()
    return text.splitlines()[0].strip() if text else ""


def _kind(row: SkillRow) -> str:
    if row.mutating is None:
        return "undeclared"
    return "WRITE" if row.mutating else "read"


_SHORT_REASON = {
    REASON_NOT_AGENT_FACING: "no (not agent-facing)",
    REASON_NOT_IN_CHAT_REGISTRY: "no (not in chat registry)",
    REASON_NAMESPACE_OFF: "no (namespace off)",
    REASON_WITHHELD: "no (withheld now)",
}


def _can_call(row: SkillRow) -> str:
    return "yes" if row.callable_now else f"no: {row.reason}"


def _can_call_short(row: SkillRow) -> str:
    return "yes" if row.callable_now else _SHORT_REASON.get(row.reason, "no")


FOLLOW_UPS = {
    "open": FollowUp("/skills 1", "open the first skill: parameters, permission, CLI verb and MCP tool"),
    "detail": FollowUp("/skills {name}", "parameters, permission, CLI verb and MCP tool"),
    "enable": FollowUp(
        "{cli} settings set chat.tool_namespaces {ns}", "offer the {ns} skills to the model (a setting, not a code change)"
    ),
    "tool": FollowUp("/tools {tool}", "its permission and what happens on a call"),
    "all": FollowUp("/skills", "list every skill again"),
    "missing": FollowUp("/skills --missing", "name the registered skills that have no SkillSpec"),
}


def ordered_rows(report: SkillsReport) -> list[SkillRow]:
    """Rows in display order (namespace, then registration order). A row's number is its place here."""
    return [r for ns in sorted({r.namespace for r in report.rows}) for r in report.rows if r.namespace == ns]


def listing_follow_ups(report: SkillsReport) -> list:
    keys: list = []
    off = next((r for r in ordered_rows(report) if not r.callable_now and r.enable_command), None)
    if off is not None:
        keys.append(("enable", {"ns": off.namespace}))
    keys.append("open" if report.rows else "all")
    if report.missing:
        keys.append("missing")
    return keys


def detail_follow_ups(row: SkillRow) -> list:
    keys: list = []
    if not row.callable_now and row.enable_command:
        keys.append(("enable", {"ns": row.namespace}))
    if row.callable_now:
        keys.append(("tool", {"tool": row.tool_name}))
    keys.append("all")
    return keys


def format_listing(report: SkillsReport, needle: str = "") -> str:
    from axiom.infra.cli_format import Column, table

    cli = _brand_cli()
    lines = [""]
    if needle and not report.rows and not report.missing:
        return f"\n  No skill matches {needle!r}. `/skills` lists them all.\n"
    head = f"{len(report.rows)} skill{'s' if len(report.rows) != 1 else ''} with a SkillSpec"
    if needle:
        head += f" matching {needle!r}"
    lines.append(f"  {head}; {report.callable_count} the chat agent can call now")
    lines.append(
        f"  {len(report.missing)} registered skill{'s have' if len(report.missing) != 1 else ' has'} NO SkillSpec "
        f"and {'are' if len(report.missing) != 1 else 'is'} not discoverable by the model"
        + ("  (`/skills --missing` names them)" if report.missing else "")
    )
    lines.append("")
    number = {r.name: i for i, r in enumerate(ordered_rows(report), 1)}
    for ns in sorted({r.namespace for r in report.rows}):
        members = [r for r in report.rows if r.namespace == ns]
        state = "enabled" if ns in report.enabled_namespaces else "not enabled"
        lines.append(f"  {ns}  ({state} in {NAMESPACES_SETTING})")
        lines.extend(
            table(
                [
                    (str(number[r.name]), r.name, _kind(r), _can_call_short(r), _first_line(r.description))
                    for r in members
                ],
                [
                    Column("#", align="right"),
                    Column("skill"),
                    Column("kind"),
                    Column("agent can call now"),
                    Column("what it does", wrap=True),
                ],
                width=120,
                indent=4,
                headers=True,
            )
        )
        fix = next((r.enable_command for r in members if r.enable_command), None)
        if fix:
            lines.append(f"    enable: {fix}")
        lines.append("")
    lines.append("  read = auto-approved; WRITE = held for approval; undeclared is treated as WRITE.")
    lines.append("  Why a skill is not callable is spelled out in `/skills <name>` (or its number: `/skills 5`).")
    lines.append(f"  `/skills <name>` shows parameters, permission, and the CLI verb and MCP tool. Enabled: "
                 f"{', '.join(report.enabled_namespaces) or 'none (chat.tool_namespaces is empty)'}")
    lines.append(f"  A skill that has not declared the agent_tool surface cannot be enabled by a setting; "
                 f"the capability must declare it. `{cli} settings set` only changes namespaces.")
    lines.append("")
    return "\n".join(lines)


def format_missing(report: SkillsReport) -> str:
    if not report.missing:
        return "\n  Every registered skill has a SkillSpec.\n"
    lines = ["", f"  {len(report.missing)} registered skill(s) with NO SkillSpec (not discoverable by the model):", ""]
    lines.extend(f"    {n}" for n in report.missing)
    lines.append("")
    lines.append("  A skill registered with register(name, fn) carries no description, inputs, side_effects or")
    lines.append("  surfaces, so no surface can project it. Register it with register_skill(SkillSpec(...)).")
    lines.append("")
    return "\n".join(lines)


def format_detail(row: SkillRow, agent=None) -> str:
    lines = ["", f"  {row.name}", f"    namespace   {row.namespace}"]
    lines.append(f"    kind        {_kind(row)}" + (
        " (held for approval)" if row.mutating is not False else " (auto-approved)"
    ))
    lines.append(f"    agent can call now   {_can_call(row)}")
    if row.enable_command:
        lines.append(f"    enable      {row.enable_command}")
    perm = None
    if agent is not None:
        try:
            explicit = agent.permissions.all()
            perm = agent.permissions.get(row.tool_name)
            tail = "" if row.tool_name in explicit else " (default)"
            lines.append(f"    permission  {perm}{tail}" + ("" if row.callable_now else "  (applies once the tool is offered)"))
        except Exception:  # noqa: BLE001
            lines.append("    permission  unknown")
    lines.append(f"    surfaces    {', '.join(row.surfaces) or 'none declared'}")
    lines.append(f"    chat tool   {row.tool_name}")
    lines.append(f"    CLI verb    {row.cli_verb or 'not declared on the cli surface'}"
                 + ("  (namespace = CLI noun, by convention)" if row.cli_verb else ""))
    if row.mcp_exposed:
        lines.append(f"    MCP tool    {row.mcp_tool or 'declared on the mcp surface (name unavailable: mcp extra not installed)'}")
    else:
        lines.append("    MCP tool    not exposed (the capability has not declared the mcp surface)")
    lines.append("")
    lines.append("  Description")
    lines.append(f"    {row.description or '(none)'}")
    lines.append("")
    lines.append("  Parameters")
    if not row.inputs:
        lines.append("    (none declared; an empty map means unknown, not \"takes no arguments\")")
    else:
        for key, shape in row.inputs.items():
            base, required = split_shape(shape)
            lines.append(f"    {key}  ({base}, {'required' if required else 'optional'})")
    lines.append(f"  Schema: {json.dumps(inputs_to_json_schema(row.inputs), sort_keys=True)[:400]}")
    lines.append("")
    return "\n".join(lines)


def to_json(report: SkillsReport) -> str:
    payload = asdict(report)
    payload["callable_count"] = report.callable_count
    return json.dumps(payload, indent=2, default=list)


# ---- completion and command ---------------------------------------------------------------------

_FLAGS = (("--json", "machine-readable output"), ("--missing", "name the skills with no SkillSpec"))


def complete_skills(agent, args_so_far: list[str]) -> list[tuple[str, str]]:
    """Skill names, namespaces and flags for the first word after ``/skills``."""
    if args_so_far:
        return []
    try:
        report = list_skills(offered=set())  # names only: no tool-table scan while the user is typing
    except Exception:  # noqa: BLE001 - completion never raises
        return list(_FLAGS)
    out: list[tuple[str, str]] = [
        (ns, "namespace") for ns in sorted({r.namespace for r in report.rows})
    ]
    out += [(r.name, _first_line(r.description)) for r in report.rows]
    out += list(_FLAGS)
    return out


def skills_data(needle: str = "") -> dict:
    """``/skills --json``, ``axi chat --skills --json`` and the ``axiom_chat__skills`` tool all return this."""
    report = filter_report(list_skills(), needle)
    payload = json.loads(to_json(report))
    payload["rows"] = [dict(r, number=i) for i, r in enumerate(_ordered_dicts(payload["rows"]), 1)]
    return payload


def _ordered_dicts(rows: list[dict]) -> list[dict]:
    return [r for ns in sorted({r["namespace"] for r in rows}) for r in rows if r["namespace"] == ns]


@command(
    "/skills",
    "Browse the platform's skills: which the chat agent can call now, and how to enable the rest",
    G_AGENT,
    usage="/skills [filter|name|number] [--missing] [--json]",
    complete=complete_skills,
    follow_ups=FOLLOW_UPS,
    cli=CliSpec(("--skills",), line="/skills", applies="headless", json=True,
                help="print the platform's skills and whether chat can call them, and exit"),
    expose=frozenset({"chat", "cli", "mcp"}),
    mcp=(McpForm("chat.skills", "/skills"),),
)
def _skills(ctx: CommandContext, args: list[str]):
    as_json = "--json" in args
    want_missing = "--missing" in args
    words = [a for a in args if not a.startswith("--")]
    needle = " ".join(words).strip()
    full = list_skills()  # one scan: the tool table is rebuilt from disk each time
    if len(words) == 1 and words[0].isdigit():
        key, why = resolve_ref(ctx.agent, "skills", words[0], fallback=lambda: [r.name for r in ordered_rows(full)])
        if key is None:
            return f"\n  {why}\n"
        needle = key
    exact = next((r for r in full.rows if r.name == needle), None)
    if exact is not None:
        report = replace(full, rows=(exact,), missing=())
        if as_json:
            return json.dumps(with_json_follow_ups(json.loads(to_json(report)), pick("/skills", *detail_follow_ups(exact))), indent=2)
        remember_list(ctx.agent, "skills", [exact.name])
        return result("/skills", format_detail(exact, ctx.agent), *detail_follow_ups(exact))
    report = filter_report(full, needle)
    if want_missing:
        if as_json:
            return json.dumps({"missing": list(report.missing), "count": len(report.missing)}, indent=2)
        return result("/skills", format_missing(report), "all")
    keys = listing_follow_ups(report)
    if as_json:
        return json.dumps(with_json_follow_ups(skills_data(needle), pick("/skills", *keys)), indent=2)
    remember_list(ctx.agent, "skills", [r.name for r in ordered_rows(report)])
    return result("/skills", format_listing(report, needle), *keys)
