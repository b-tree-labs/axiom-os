# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/mcp``: the MCP surface as implemented, read from the files ``mcp status`` reads.

Two separate questions:

* What MCP servers does Axiom *provide*, and which harnesses are they registered with? Answered by the
  same functions the CLI uses: ``mcp.harnesses.survey`` / ``complaints`` for the one composed server
  (memory is a built-in of it; the standalone memory server was retired), ``mcp.install.client_capabilities``
  for the export-control routability matrix.
* Can the chat agent *call* MCP servers? Only if a tool in the table it is offered is backed by an MCP
  client. That is checked against ``get_all_tools()`` rather than asserted, so the answer changes the day
  the platform grows such a tool.

Nothing is shelled out, and no command line or environment block is printed: a harness entry can carry
tokens, so only names, states and config file paths appear.
"""

from __future__ import annotations

from ..command_registry import CommandContext, McpForm, command
from ..follow_ups import FollowUp, result

G_INSPECT = "Inspect"

#: Packages whose callables mean "this tool speaks MCP as a client": the MCP SDK itself. Axiom's own
#: ``mcp`` extension is the SERVER side; a chat tool that merely lives there is reported too, because it is
#: the only place an in-tree MCP client could appear.
_MCP_CLIENT_ROOTS = ("mcp.", "axiom.extensions.builtins.mcp.")


_MAX_COMPLAINTS = 5


def _brand_cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001 - a label never takes the command down
        return "axi"


def mcp_backed_tools(all_tools: dict) -> list[str]:
    """Names of tools in the chat table whose handler lives in an MCP package."""
    found = []
    for name, tool in sorted(all_tools.items()):
        mod = getattr(getattr(tool, "handler", None), "__module__", "") or ""
        if mod == "mcp" or mod.startswith(_MCP_CLIENT_ROOTS):
            found.append(name)
    return found


def _composed_state(view) -> str:
    if view is None:
        return "not registered"
    if not view.command:
        return "unreadable config"
    return "registered" if view.command_exists else "broken (command missing)"


def format_mcp() -> str:
    from axiom.extensions.builtins.mcp import harnesses, install
    from axiom.infra.cli_format import Column, table

    from .. import tools as chat_tools

    cli = _brand_cli()
    composed_name = install.resolve_server()[0]
    lines = ["", "  MCP servers Axiom provides"]
    lines.extend(
        table(
            [
                (
                    composed_name,
                    "the one server: every extension's tools (memory included), resources and prompts",
                ),
            ],
            [Column(), Column(wrap=True)],
            indent=4,
        )
    )
    lines.append("")

    try:
        views = {v.tool: v for v in harnesses.survey()}
        said = harnesses.complaints(views=list(views.values()))
        survey_error = ""
    except Exception as exc:  # noqa: BLE001
        views, said, survey_error = {}, [], f"{type(exc).__name__}: {exc}"

    rows = []
    for cap in install.client_capabilities():
        client = cap["client"]
        composed = "unknown" if survey_error else _composed_state(views.get(client))
        ec = "EC-routable" if cap["ec_routable"] else "EC-blocked"
        rows.append((client, composed, ec))
    lines.append("  Registered with harnesses")
    lines.extend(
        table(
            rows,
            [
                Column("harness"),
                Column(composed_name),
                Column("export control"),
            ],
            width=96,
            indent=4,
            headers=True,
        )
    )
    lines.append("")
    lines.append("  EC-blocked: the harness's model cannot be placed in-enclave, so the server withholds")
    lines.append("  export-controlled tool output from it. Routable harnesses still need --route-model.")
    if survey_error:
        lines.append(f"  Composed-server registration could not be read: {survey_error}")
    # Several stamps can say the same thing (one per unidentified process); say it once, then count.
    distinct = list(dict.fromkeys(said))
    for complaint in distinct[:_MAX_COMPLAINTS]:
        lines.append(f"  ! {complaint}")
    if len(distinct) > _MAX_COMPLAINTS:
        lines.append(f"  ! ...and {len(distinct) - _MAX_COMPLAINTS} more; `{cli} mcp status` lists them")
    lines.append("")

    lines.append("  Can this chat call MCP servers?")
    backed = mcp_backed_tools(chat_tools.get_all_tools())
    if backed:
        lines.append("    Yes, through: " + ", ".join(backed))
    else:
        lines.append("    No. The chat agent cannot call external MCP servers: its tools are the built-ins,")
        lines.append("    chat extensions, manifest tools and (when enabled) skill namespaces listed by")
        lines.append("    /tools, and none of them is an MCP client. Axiom is an MCP server to other")
        lines.append("    harnesses; it is not an MCP client to others.")
    lines.append("")
    lines.append(f"  {cli} mcp status   tools and contributors, per-harness config paths and running versions")
    lines.append(f"  {cli} mcp install  register the composed server with detected harnesses")
    lines.append("")
    return "\n".join(lines)


FOLLOW_UPS = {
    "tools": FollowUp("/tools", "the tools this chat can call; none of them is an MCP client"),
    "install": FollowUp("{cli} mcp install", "register the composed server with the harnesses detected here"),
    "status": FollowUp("{cli} mcp status", "tools and contributors, per-harness config paths and running versions"),
}


def mcp_data() -> dict:
    """The MCP surface as data: servers, per-harness registration, whether chat is an MCP client.
    The ``axiom_chat__mcp`` tool returns this. Config paths and credentials are not included."""
    from axiom.extensions.builtins.mcp import harnesses, install

    from .. import tools as chat_tools

    composed_name = install.resolve_server()[0]
    try:
        views = {v.tool: v for v in harnesses.survey()}
        error = ""
    except Exception as exc:  # noqa: BLE001
        views, error = {}, f"{type(exc).__name__}: {exc}"
    rows = []
    for cap in install.client_capabilities():
        client = cap["client"]
        rows.append(
            {
                "harness": client,
                "composed": "unknown" if error else _composed_state(views.get(client)),
                "export_control": "EC-routable" if cap["ec_routable"] else "EC-blocked",
            }
        )
    backed = mcp_backed_tools(chat_tools.get_all_tools())
    return {
        "servers": [
            {"name": composed_name, "provides": "every extension's tools (memory included), resources and prompts"},
        ],
        "harnesses": rows,
        "chat_is_mcp_client": bool(backed),
        "mcp_backed_tools": backed,
        "survey_error": error,
    }


@command(
    "/mcp",
    "MCP surface: the servers Axiom provides, which harnesses use them, export-control routing",
    G_INSPECT,
    follow_ups=FOLLOW_UPS,
    expose=frozenset({"chat", "cli", "mcp"}),
    mcp=(McpForm("chat.mcp", "/mcp"),),
)
def _mcp(ctx: CommandContext, args: list[str]):
    return result("/mcp", format_mcp(), "tools", "install", "status")
