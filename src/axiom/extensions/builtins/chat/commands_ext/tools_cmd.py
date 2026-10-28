# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/tools``: every tool the agent can call, and what happens when it does.

Nothing here keeps a list of its own. The rows come from ``get_all_tools()``, the same table the chat
turn offers the model, so a tool that is offered is listed and a tool that is not offered is not. What
happens on a call is read from the two things the approval gate itself consults: the tool's
``ActionCategory`` (WRITE is held for approval, READ is auto-approved by ``ApprovalGate.submit``) and
``agent.permissions`` (``allow`` skips the prompt, ``deny`` refuses, absent means the default ``ask``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from axiom.infra.orchestrator.actions import ActionCategory

from ..command_registry import CliSpec, CommandContext, McpForm, command
from ..follow_ups import FollowUp, remember_list, resolve_ref, result, with_json_follow_ups
from ..tools import (
    TOOL_SOURCE_BUILTIN,
    TOOL_SOURCE_CHAT_EXT,
    TOOL_SOURCE_EXTENSION,
    TOOL_SOURCE_MANIFEST,
    TOOL_SOURCE_SKILL,
    ToolDef,
)

G_AGENT = "Agent"

ORIGIN_BUILTIN = "Built-in"
ORIGIN_EXT = "Chat extension (tools_ext)"
ORIGIN_MANIFEST = "Extension manifest"
ORIGIN_USER = "Installed extension"
ORIGIN_OTHER = "Other"
ORIGIN_SKILL = "Skill"  # rendered as "Skill: <namespace>"

_ORIGIN_ORDER = (ORIGIN_BUILTIN, ORIGIN_EXT, ORIGIN_MANIFEST, ORIGIN_USER, ORIGIN_OTHER)


def _brand_cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001 - a label never takes the command down
        return "axi"


@dataclass(frozen=True)
class ToolRow:
    name: str
    origin: str  # a group heading, e.g. "Built-in" or "Skill: press"
    mutates: bool
    permission: str  # allow | ask | deny
    is_default: bool  # no override: permission is the default, not a setting
    effect: str  # what a call does right now
    tool: ToolDef


_SOURCE_LABEL = {
    TOOL_SOURCE_BUILTIN: ORIGIN_BUILTIN,
    TOOL_SOURCE_CHAT_EXT: ORIGIN_EXT,
    TOOL_SOURCE_MANIFEST: ORIGIN_MANIFEST,
    TOOL_SOURCE_EXTENSION: ORIGIN_USER,
}


def origin_of(tool: ToolDef, name: str) -> str:
    """The group heading for a tool, from the source ``get_all_tools`` stamped on it."""
    if tool.source == TOOL_SOURCE_SKILL:
        from ..skill_tools import tool_to_skill_name

        return f"{ORIGIN_SKILL}: {tool_to_skill_name(name).split('.')[0]}"
    return _SOURCE_LABEL.get(tool.source, ORIGIN_OTHER)


def _effect(mutates: bool, permission: str, session_allowed: bool) -> str:
    if permission == "deny":
        return "refused"
    if permission == "allow":
        return "runs"
    if session_allowed:
        return "runs (allowed this session)"
    return "asks" if mutates else "runs"


def tool_rows(agent) -> list[ToolRow]:
    from ..tools import get_all_tools

    all_tools = get_all_tools()
    explicit = agent.permissions.all()
    session_allowed = set(agent.allowlisted_tools())
    rows = []
    for name, tool in all_tools.items():
        mutates = tool.category is ActionCategory.WRITE
        perm = agent.permissions.get(name)
        rows.append(
            ToolRow(
                name=name,
                origin=origin_of(tool, name),
                mutates=mutates,
                permission=perm,
                is_default=name not in explicit,
                effect=_effect(mutates, perm, name in session_allowed),
                tool=tool,
            )
        )
    return rows


def _origin_key(origin: str) -> tuple[int, str]:
    if origin in _ORIGIN_ORDER:
        return (_ORIGIN_ORDER.index(origin), origin)
    return (len(_ORIGIN_ORDER), origin)


def _perm_cell(row: ToolRow) -> str:
    return f"{row.permission} (default)" if row.is_default else row.permission


def _kind_cell(row: ToolRow) -> str:
    return "WRITE" if row.mutates else "read"


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


#: Follow-ups this command can offer (declared once, here). ``{tool}`` / ``{ns}`` are filled when emitted.
FOLLOW_UPS = {
    "refused": FollowUp(
        "/permissions set {tool} ask", "{tool} is refused; ask makes it prompt before each call"
    ),
    "asks": FollowUp(
        "/permissions set {tool} allow", "{tool} asks before each call; allow skips the prompt"
    ),
    "enable_namespace": FollowUp(
        "{cli} settings set chat.tool_namespaces {ns}",
        "skill tools are off; this offers the {ns} skills to the model",
    ),
    "detail": FollowUp("/tools 1", "open the first row: arguments, last call, last refusal"),
    "all": FollowUp("/tools", "list every tool again"),
    "skills": FollowUp("/skills", "see which skills the agent can call and how to enable the rest"),
}


def ordered(rows: list[ToolRow]) -> list[ToolRow]:
    """Rows in display order (origin group, then name). The number a row shows is its place in this list."""
    return [
        r
        for origin in sorted({r.origin for r in rows}, key=_origin_key)
        for r in sorted((r for r in rows if r.origin == origin), key=lambda r: r.name)
    ]


def follow_ups_for_listing(agent, rows: list[ToolRow]) -> list:
    """Which follow-ups this listing earns: the first refused tool, the first default-ask write, skills off."""
    keys: list = []
    shown = ordered(rows)
    refused = next((r for r in shown if r.permission == "deny"), None)
    if refused:
        keys.append(("refused", {"tool": refused.name}))
    asking = next((r for r in shown if r.mutates and r.permission == "ask"), None)
    if asking:
        keys.append(("asks", {"tool": asking.name}))
    from .. import tools as chat_tools

    if not chat_tools._skill_namespaces():  # noqa: SLF001
        available = _available_namespaces()
        if available:
            keys.append(("enable_namespace", {"ns": sorted(available)[0]}))
    keys.append("detail" if shown else "all")
    return keys


def follow_ups_for_detail(row: ToolRow) -> list:
    keys: list = []
    if row.permission == "deny":
        keys.append(("refused", {"tool": row.name}))
    elif row.mutates and row.permission == "ask":
        keys.append(("asks", {"tool": row.name}))
    keys.append("all")
    return keys


def _footer(agent) -> list[str]:
    from .. import tools as chat_tools

    cli = _brand_cli()
    namespaces = chat_tools._skill_namespaces()  # noqa: SLF001
    lines: list[str] = []
    if namespaces:
        lines.append(f"  Skill namespaces enabled (chat.tool_namespaces): {', '.join(namespaces)}")
    else:
        lines.append(
            "  Skill tools are not offered: chat.tool_namespaces is empty (the default), so only the"
        )
        lines.append("  tools above reach the model.")
    available = _available_namespaces()
    if available:
        listing = ", ".join(f"{ns} ({n})" for ns, n in sorted(available.items()))
        lines.append(f"  Namespaces with agent-facing capabilities: {listing}")
    lines.append(f"  Enable with: {cli} settings set chat.tool_namespaces <ns>[,<ns>]")
    lines.append("  Change a permission: /permissions set <tool> allow|ask|deny")
    lines.append("  Open a row by its number or name: /tools 3")
    return lines


def _available_namespaces() -> dict[str, int]:
    """Namespaces that have capabilities which declared the ``agent_tool`` surface."""
    try:
        from axiom.infra.capability_projection import exposed_on

        from .. import tools as chat_tools
        from ..skill_tools import AGENT_SURFACE

        counts: dict[str, int] = {}
        for name, spec in chat_tools._skill_registry().specs().items():  # noqa: SLF001
            if exposed_on(spec, AGENT_SURFACE):
                ns = name.split(".")[0]
                counts[ns] = counts.get(ns, 0) + 1
        return counts
    except Exception:  # noqa: BLE001 - unknown is reported by omission, never as zero
        return {}


def format_listing(agent, needle: str = "", rows: list[ToolRow] | None = None) -> str:
    from axiom.infra.cli_format import Column, table

    rows = tool_rows(agent) if rows is None else rows
    if needle:
        n = needle.lower()
        rows = [r for r in rows if n in r.name.lower() or n in r.origin.lower()]
        if not rows:
            return f"\n  No tool matches {needle!r}. `/tools` lists them all.\n"
    number = {r.name: i for i, r in enumerate(ordered(rows), 1)}
    lines = ["", f"  {len(rows)} tool{'s' if len(rows) != 1 else ''} the agent can call", ""]
    for origin in sorted({r.origin for r in rows}, key=_origin_key):
        members = sorted((r for r in rows if r.origin == origin), key=lambda r: r.name)
        lines.append(f"  {origin}")
        lines.extend(
            table(
                [(str(number[r.name]), r.name, _perm_cell(r), _kind_cell(r), r.effect) for r in members],
                [
                    Column("#", align="right"),
                    Column("tool", wrap=True),
                    Column("permission"),
                    Column("kind"),
                    Column("on a call"),
                ],
                width=96,
                indent=4,
            )
        )
        lines.append("")
    lines.append("  read = auto-approved; WRITE = held for approval unless allowed. Every call also")
    lines.append("  consults the declared-authority hook; a refusal there is journaled.")
    lines.extend(_footer(agent))
    lines.append("")
    return "\n".join(lines)


# ---- detail -------------------------------------------------------------------------------------


def _schema_lines(params: dict) -> list[str]:
    props = (params or {}).get("properties") or {}
    if not props:
        return ["    (takes no arguments)"]
    required = set((params or {}).get("required") or [])
    out = []
    for key, spec in props.items():
        kind = spec.get("type", "any") if isinstance(spec, dict) else "any"
        mark = "required" if key in required else "optional"
        desc = _first_line(spec.get("description", "")) if isinstance(spec, dict) else ""
        out.append(f"    {key}  ({kind}, {mark})" + (f"  {desc}" if desc else ""))
    return out


def last_called_in_session(agent, name: str) -> str | None:
    """Timestamp of the newest tool-result message for ``name`` in this session, or None."""
    for msg in reversed(list(getattr(agent.session, "messages", []))):
        if getattr(msg, "role", "") != "tool":
            continue
        if any(tc.get("name") == name for tc in (getattr(msg, "tool_calls", None) or [])):
            return msg.timestamp
    return None


def last_refusal(name: str) -> dict | None:
    """The newest refusal of ``name`` in the action ledger, or None.

    Raises on an unreadable ledger so the caller can say "unknown" instead of "none".
    """
    from axiom.infra.authority import TOOL_INVOKE_INTENT
    from axiom.policy.action_ledger import search_actions

    result = search_actions(
        agent="chat", op_class=TOOL_INVOKE_INTENT, outcome="refused", text=name, limit=50
    )
    if "error" in result:
        raise RuntimeError(result["error"])
    for row in result["actions"]:  # an exact name match; `text` is a substring filter
        if row.get("name") == name:
            return row
    return None


def format_detail(agent, row: ToolRow) -> str:
    from axiom.infra.cli_format import relative_age

    t = row.tool
    lines = ["", f"  {row.name}", f"    from        {row.origin}"]
    lines.append(f"    kind        {_kind_cell(row)} ({'held for approval' if row.mutates else 'auto-approved'})")
    lines.append(f"    permission  {_perm_cell(row)}")
    lines.append(f"    on a call   {row.effect}")
    lines.append("")
    lines.append("  Description")
    for ln in (t.description or "(none)").strip().splitlines():
        lines.append(f"    {ln.strip()}")
    lines.append("")
    lines.append("  Arguments")
    lines.extend(_schema_lines(t.parameters))
    lines.append("")
    when = last_called_in_session(agent, row.name)
    lines.append(
        f"  Last called   {relative_age(when)} (this session)" if when
        else "  Last called   not called in this session"
    )
    try:
        refusal = last_refusal(row.name)
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  Last refused  unknown: the action ledger could not be read ({exc})")
    else:
        if refusal:
            why = refusal.get("refusing_rule") or "refused"
            lines.append(f"  Last refused  {relative_age(str(refusal.get('timestamp') or ''))}: {why}")
        else:
            lines.append("  Last refused  no refusal recorded in the action ledger")
    lines.append("  Last called reads this session only; earlier sessions are not searched. The action ledger holds refusals.")
    lines.append(f"  Raw schema: {json.dumps(t.parameters or {}, sort_keys=True)[:400]}")
    lines.append("")
    return "\n".join(lines)


def tools_data(agent, needle: str = "") -> dict:
    """The tool table as JSON-serializable data: what ``/tools --json``, ``axi chat --tools --json`` and
    the ``axiom_chat__tools`` MCP tool all return."""
    rows = ordered(tool_rows(agent))
    if needle:
        n = needle.lower()
        rows = [r for r in rows if n in r.name.lower() or n in r.origin.lower()]
    return {
        "tools": [
            {
                "number": i,
                "name": r.name,
                "origin": r.origin,
                "kind": "write" if r.mutates else "read",
                "permission": r.permission,
                "permission_is_default": r.is_default,
                "on_a_call": r.effect,
                "description": _first_line(r.tool.description),
            }
            for i, r in enumerate(rows, 1)
        ],
        "count": len(rows),
    }


@command(
    "/tools",
    "List the tools the agent can call: permission, whether it writes, where it comes from",
    G_AGENT,
    usage="/tools [filter|name|number] [--json]",
    follow_ups=FOLLOW_UPS,
    cli=CliSpec(
        ("--tools",), line="/tools", applies="headless", json=True,
        help="print the tools the agent can call and exit",
    ),
    expose=frozenset({"chat", "cli", "mcp"}),
    mcp=(McpForm("chat.tools"),),
)
def _tools(ctx: CommandContext, args: list[str]):
    as_json = "--json" in args
    words = [a for a in args if a != "--json"]
    needle = " ".join(words).strip()
    rows = tool_rows(ctx.agent)  # one scan per command: the table is rebuilt from disk each time
    if len(words) == 1 and words[0].isdigit():
        key, why = resolve_ref(ctx.agent, "tools", words[0], fallback=lambda: [r.name for r in ordered(rows)])
        if key is None:
            return f"\n  {why}\n"
        needle = key
    if as_json:
        data = tools_data(ctx.agent, "" if any(r.name == needle for r in rows) else needle)
        if any(r.name == needle for r in rows):
            data["tools"] = [t for t in data["tools"] if t["name"] == needle]
            data["count"] = len(data["tools"])
        keys = follow_ups_for_listing(ctx.agent, rows)
        from ..follow_ups import pick

        return json.dumps(with_json_follow_ups(data, pick("/tools", *keys)), indent=2)
    exact = [r for r in rows if r.name == needle]
    if exact:
        remember_list(ctx.agent, "tools", [exact[0].name])
        return result(
            "/tools", format_detail(ctx.agent, exact[0]), *follow_ups_for_detail(exact[0])
        )
    text = format_listing(ctx.agent, needle, rows)
    shown = ordered(
        [r for r in rows if not needle or needle.lower() in r.name.lower() or needle.lower() in r.origin.lower()]
    )
    remember_list(ctx.agent, "tools", [r.name for r in shown])
    if not shown:
        return result("/tools", text, "all")
    return result("/tools", text, *follow_ups_for_listing(ctx.agent, shown))
