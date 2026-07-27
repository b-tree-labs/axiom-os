# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom MCP server — expose classroom tools to Claude Code / Cursor / any MCP client.

Adoption prong 2 (spec-classroom.md §2.10a, prd-classroom.md §10.2):
instructors already comfortable in Claude Code should get classroom
data *without* leaving the tool they trust. This server wraps the
existing classroom operations as MCP tools.

Tools exposed:
  axiom_classroom_list_sessions      — list active prep/classroom sessions
  axiom_classroom_prep_status        — show prep checklist for a classroom
  axiom_classroom_list_tickets       — help-ticket queue
  axiom_classroom_list_signals       — SCAN signals (stuck students, etc.)

Invocation: this module exposes an `async def run()` entry point that
serves stdio MCP. Claude Code config:

    {
      "mcpServers": {
        "axiom-classroom": {
          "command": "python",
          "args": ["-m", "axiom.extensions.builtins.classroom.mcp_server"]
        }
      }
    }

No auth in this MVP — stdio local-only. Future: wire to the
cryptographic provenance + bipartite access check so tools respect
the same policy coordinate as direct CLI access.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# ---------------------------------------------------------------------------
# Runtime paths — mirrors cli.py
# ---------------------------------------------------------------------------


def _runtime_root() -> Path:
    override = os.environ.get("AXIOM_RUNTIME_ROOT")
    if override:
        return Path(override)
    try:
        from axiom import REPO_ROOT  # type: ignore

        return Path(REPO_ROOT) / "runtime"
    except Exception:
        return Path.cwd() / "runtime"


def _load_classroom_file(classroom_id: str) -> dict | None:
    """Read-only classroom state from the operational store (data only)."""
    from .operational_store import load_classroom_data

    return load_classroom_data(classroom_id)


def _load_course_file(course_id: str) -> dict | None:
    """Read-only course state from the operational store (data only)."""
    from .operational_store import load_course_data

    return load_course_data(course_id)


# ---------------------------------------------------------------------------
# Tool implementations (pure functions over persisted state)
# ---------------------------------------------------------------------------


def list_sessions() -> dict:
    """Return all classroom + course sessions from the operational store."""
    from .operational_store import _reg

    registry = _reg()

    classrooms = []
    for art in registry.list(kind="classroom"):
        data = art.data
        classrooms.append({
            "id": data.get("id", art.name),
            "slug": data.get("slug"),
            "title": data.get("title"),
            "instructor_id": data.get("instructor_id"),
            "course_id": data.get("course_id"),
        })

    courses = []
    for art in registry.list(kind="course"):
        data = art.data
        courses.append({
            "id": data.get("id", art.name),
            "slug": data.get("slug"),
            "title": data.get("title"),
        })

    return {"classrooms": classrooms, "courses": courses}


def prep_status(classroom_id: str) -> dict:
    """Return unified prep checklist: course template steps + classroom instance steps."""
    classroom = _load_classroom_file(classroom_id)
    if classroom is None:
        return {"error": f"classroom '{classroom_id}' not found"}
    course = _load_course_file(classroom["course_id"])
    if course is None:
        return {"error": f"course {classroom['course_id']} not found"}

    return {
        "classroom_id": classroom_id,
        "slug": classroom.get("slug"),
        "course_slug": classroom.get("course_slug"),
        "course_version": classroom.get("course_version"),
        "course_steps": course.get("steps", []),
        "classroom_steps": classroom.get("steps", []),
    }


def list_tickets(classroom_id: str | None = None) -> dict:
    """Return open + in-progress help tickets."""
    tickets_file = _runtime_root() / "classrooms" / (classroom_id or "_all") / "tickets.json"
    if not tickets_file.exists():
        return {"tickets": []}
    data = json.loads(tickets_file.read_text())
    return {"tickets": [t for t in data if t.get("status") in ("open", "in_progress")]}


def list_signals(classroom_id: str) -> dict:
    """Return SCAN signals for a classroom."""
    signals_file = _runtime_root() / "classrooms" / classroom_id / "signals.json"
    if not signals_file.exists():
        return {"signals": []}
    return {"signals": json.loads(signals_file.read_text())}


# ---------------------------------------------------------------------------
# MCP server wiring
# ---------------------------------------------------------------------------


_TOOLS = [
    Tool(
        name="axiom_classroom_list_sessions",
        description=(
            "List all classroom and course sessions present on the local Axiom node. "
            "Returns arrays of classrooms (with id, slug, title, instructor_id, "
            "course_id) and courses (with id, slug, title)."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="axiom_classroom_prep_status",
        description=(
            "Show the unified prep checklist for a single classroom — both the "
            "course-template steps (manifest, corpus, prompt, assessments, rails) "
            "and the classroom-instance steps (course selected, RAG policy, LMS, "
            "dry-run). Input: classroom_id (uuid)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "classroom_id": {"type": "string", "description": "Classroom UUID"},
            },
            "required": ["classroom_id"],
        },
    ),
    Tool(
        name="axiom_classroom_list_tickets",
        description=(
            "Return open + in-progress help tickets (students who invoked /help in chat). "
            "Optional classroom_id to filter; otherwise returns all local tickets."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "classroom_id": {
                    "type": "string",
                    "description": "Optional classroom UUID to filter by",
                },
            },
        },
    ),
    Tool(
        name="axiom_classroom_list_signals",
        description=(
            "Return SCAN signals for a classroom — stuck students, misconceptions, "
            "low/high engagement, objective gaps. Input: classroom_id (uuid)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "classroom_id": {"type": "string", "description": "Classroom UUID"},
            },
            "required": ["classroom_id"],
        },
    ),
]


_HANDLERS = {
    "axiom_classroom_list_sessions": lambda args: list_sessions(),
    "axiom_classroom_prep_status":
        lambda args: prep_status(args["classroom_id"]),
    "axiom_classroom_list_tickets":
        lambda args: list_tickets(args.get("classroom_id")),
    "axiom_classroom_list_signals":
        lambda args: list_signals(args["classroom_id"]),
}


def build_server() -> Server:
    """Construct the MCP Server instance with tools + handlers wired."""
    server: Server = Server("axiom-classroom")

    @server.list_tools()  # type: ignore[misc]
    async def _list_tools() -> list[Tool]:
        return _TOOLS

    @server.call_tool()  # type: ignore[misc]
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        handler = _HANDLERS.get(name)
        if handler is None:
            return [TextContent(type="text", text=json.dumps({
                "error": f"unknown tool: {name}",
            }))]
        try:
            result = handler(arguments or {})
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    return server


async def run() -> None:
    """Serve MCP over stdio. Entry point for `python -m <this module>`."""
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(run())
