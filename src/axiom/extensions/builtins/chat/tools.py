# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tool registry for neut chat.

Each tool wraps an existing engine method and is classified as read-only
(auto-approved) or write (requires human confirmation). Tools are exposed
to the LLM as function definitions for tool-use.

The registry supports hot-reloading of extension tools from tools_ext/.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

from axiom.infra.orchestrator.actions import ActionCategory

logger = logging.getLogger(__name__)

_EXT_DIR = Path(__file__).parent / "tools_ext"
_ext_cache: dict[str, float] = {}  # module_name -> last_mtime


@dataclass
class ToolDef:
    """Definition of a chat tool."""

    name: str
    description: str
    category: ActionCategory
    parameters: dict[str, Any] = field(default_factory=dict)
    handler: Callable[..., dict[str, Any]] | None = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# Built-in tool definitions
# ---------------------------------------------------------------------------


def _build_tool_defs() -> dict[str, ToolDef]:
    """Build the built-in tool registry with definitions (handlers bound lazily)."""
    tools = {}

    # --- Read-only tools ---

    tools["query_docs"] = ToolDef(
        name="query_docs",
        description="Check publisher status of tracked documents.",
        category=ActionCategory.READ,
        parameters={
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "Optional source file path to query status for.",
                },
            },
        },
    )

    tools["signal_status"] = ToolDef(
        name="signal_status",
        description="Show inbox/processed/draft counts for the signal pipeline.",
        category=ActionCategory.READ,
        parameters={"type": "object", "properties": {}},
    )

    tools["list_providers"] = ToolDef(
        name="list_providers",
        description="List all registered publisher providers by category.",
        category=ActionCategory.READ,
        parameters={"type": "object", "properties": {}},
    )

    tools["doc_check_links"] = ToolDef(
        name="doc_check_links",
        description="Verify all cross-document links resolve.",
        category=ActionCategory.READ,
        parameters={"type": "object", "properties": {}},
    )

    tools["doc_diff"] = ToolDef(
        name="doc_diff",
        description="Show documents changed since last publish.",
        category=ActionCategory.READ,
        parameters={"type": "object", "properties": {}},
    )

    # --- Write tools ---

    tools["signal_ingest"] = ToolDef(
        name="signal_ingest",
        description="Run extractors on inbox data to extract signals.",
        category=ActionCategory.WRITE,
        parameters={
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["gitlab", "voice", "freetext", "transcript", "all"],
                    "description": "Which source(s) to ingest.",
                },
            },
        },
    )

    tools["doc_generate"] = ToolDef(
        name="doc_generate",
        description="Generate a .docx artifact from a markdown source file.",
        category=ActionCategory.WRITE,
        parameters={
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "Path to the markdown source file.",
                },
            },
            "required": ["source"],
        },
    )

    tools["doc_publish"] = ToolDef(
        name="doc_publish",
        description="Generate and publish a document to configured storage.",
        category=ActionCategory.WRITE,
        parameters={
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "Path to the markdown source file.",
                },
                "storage": {
                    "type": "string",
                    "description": "Override storage provider (e.g., 'local', 'onedrive').",
                },
                "draft": {
                    "type": "boolean",
                    "description": "Publish as draft.",
                },
            },
            "required": ["source"],
        },
    )

    tools["write_file"] = ToolDef(
        name="write_file",
        description="Write content to a file. Creates parent directories if needed.",
        category=ActionCategory.WRITE,
        parameters={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute or project-relative path to write.",
                },
                "content": {
                    "type": "string",
                    "description": "The text content to write to the file.",
                },
            },
            "required": ["file_path", "content"],
        },
    )

    tools["write_inbox_note"] = ToolDef(
        name="write_inbox_note",
        description="Drop a text note into the sense inbox.",
        category=ActionCategory.WRITE,
        parameters={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The note text to save.",
                },
            },
            "required": ["text"],
        },
    )

    return tools


# Global built-in registry
BUILTIN_TOOLS: dict[str, ToolDef] = _build_tool_defs()

# Backward-compatible alias
TOOL_REGISTRY: dict[str, ToolDef] = BUILTIN_TOOLS


# ---------------------------------------------------------------------------
# Extension tool scanning
# ---------------------------------------------------------------------------


def _scan_extensions() -> dict[str, ToolDef]:
    """Scan tools_ext/ for tool modules. Returns discovered extension tools."""
    ext_tools: dict[str, ToolDef] = {}
    if not _EXT_DIR.is_dir():
        return ext_tools

    for info in pkgutil.iter_modules([str(_EXT_DIR)]):
        mod_path = _EXT_DIR / f"{info.name}.py"
        if not mod_path.exists():
            continue

        mod_name = f"axiom.extensions.builtins.chat.tools_ext.{info.name}"

        try:
            mtime = mod_path.stat().st_mtime

            # Only reload if file changed
            if mod_name in _ext_cache and _ext_cache[mod_name] == mtime:
                mod = sys.modules.get(mod_name)
                if mod is None:
                    mod = _load_module_from_path(mod_name, mod_path)
            else:
                if mod_name in sys.modules:
                    mod = importlib.reload(sys.modules[mod_name])
                else:
                    mod = _load_module_from_path(mod_name, mod_path)
                _ext_cache[mod_name] = mtime

            for tool_def in getattr(mod, "TOOLS", []):
                ext_tools[tool_def.name] = tool_def

        except Exception as e:
            logger.warning("Failed to load extension %s: %s", info.name, e)
            continue

    return ext_tools


def _load_module_from_path(mod_name: str, mod_path: Path):
    """Load a module directly from its file path."""
    import importlib.util
    import types

    # Ensure parent packages exist in sys.modules
    parts = mod_name.rsplit(".", 1)
    if len(parts) == 2:
        parent_name = parts[0]
        if parent_name not in sys.modules:
            parent_pkg = types.ModuleType(parent_name)
            parent_pkg.__path__ = [str(mod_path.parent)]
            parent_pkg.__package__ = parent_name
            sys.modules[parent_name] = parent_pkg

    spec = importlib.util.spec_from_file_location(mod_name, str(mod_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {mod_name} from {mod_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def get_all_tools() -> dict[str, ToolDef]:
    """Return built-in + core extension + user extension tools. Called each turn."""
    all_tools = dict(BUILTIN_TOOLS)
    all_tools.update(_scan_extensions())
    # Scan user-space extensions (from .neut/extensions/ and ~/.neut/extensions/)
    try:
        from axiom.extensions.discovery import discover_and_load_chat_tools

        for tool_def in discover_and_load_chat_tools():
            all_tools[tool_def.name] = tool_def
    except Exception as e:
        logger.debug("User extension scan skipped: %s", e)
    return all_tools


# ---------------------------------------------------------------------------
# Tool definitions in OpenAI format
# ---------------------------------------------------------------------------


def get_tool_definitions() -> list[dict[str, Any]]:
    """Return tool definitions in OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in get_all_tools().values()
    ]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


def execute_tool(name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Execute a tool by name and return the result.

    Checks extension tools first, then falls back to built-in handlers.
    """
    # Check core extension tools (tools_ext/)
    ext_tools = _scan_extensions()
    if name in ext_tools:
        mod_name = None
        for info in pkgutil.iter_modules([str(_EXT_DIR)]):
            mod = sys.modules.get(f"axiom.extensions.builtins.chat.tools_ext.{info.name}")
            if mod and any(t.name == name for t in getattr(mod, "TOOLS", [])):
                mod_name = f"axiom.extensions.builtins.chat.tools_ext.{info.name}"
                break

        if mod_name and mod_name in sys.modules:
            mod = sys.modules[mod_name]
            handler = getattr(mod, "execute", None)
            if handler:
                try:
                    return handler(name, params)
                except Exception as e:
                    return {"error": f"Extension tool {name} failed: {e}"}

    # Check user-space extension tools
    try:
        from axiom.extensions.discovery import execute_extension_tool

        result = execute_extension_tool(name, params)
        if result is not None:
            return result
    except Exception as e:
        logger.debug("User extension execution skipped: %s", e)

    # Built-in tool handlers (lazy imports)
    if name == "query_docs":
        from axiom.extensions.builtins.publishing.engine import PublisherEngine

        engine = PublisherEngine()
        source = Path(params["file"]) if params.get("file") else None
        docs = engine.status(source)
        return {
            "documents": [
                {
                    "doc_id": d.doc_id,
                    "status": d.status,
                    "version": d.published.version
                    if d.published
                    else (d.active_draft.version if d.active_draft else ""),
                }
                for d in docs
            ]
        }

    elif name == "signal_status":
        from axiom.extensions.builtins.signals.cli import (
            DRAFTS_DIR,
            INBOX_PROCESSED,
            INBOX_RAW,
        )

        counts: dict[str, int] = {}
        if INBOX_RAW.exists():
            for child in INBOX_RAW.iterdir():
                if child.is_dir():
                    n = sum(1 for f in child.rglob("*") if f.is_file() and f.name != ".gitkeep")
                    if n:
                        counts[child.name] = n
                elif child.is_file() and child.name != ".gitkeep":
                    counts["root"] = counts.get("root", 0) + 1
        processed = 0
        if INBOX_PROCESSED.exists():
            processed = sum(
                1 for f in INBOX_PROCESSED.rglob("*") if f.is_file() and f.name != ".gitkeep"
            )
        drafts = 0
        if DRAFTS_DIR.exists():
            drafts = len(list(DRAFTS_DIR.glob("changelog_*.md")))
        return {"inbox_raw": counts, "processed": processed, "drafts": drafts}

    elif name == "list_providers":
        from axiom.extensions.builtins.publishing.engine import PublisherEngine

        engine = PublisherEngine()
        return engine.list_providers()

    elif name == "doc_check_links":
        from axiom.extensions.builtins.publishing.engine import PublisherEngine

        engine = PublisherEngine()
        return engine.check_links()

    elif name == "doc_diff":
        from axiom.extensions.builtins.publishing.engine import PublisherEngine

        engine = PublisherEngine()
        return {"changed": engine.diff()}

    elif name == "doc_generate":
        from axiom.extensions.builtins.publishing.engine import PublisherEngine

        engine = PublisherEngine()
        source = Path(params["source"])
        output = engine.generate(source)
        return {"output": str(output), "exists": output.exists()}

    elif name == "doc_publish":
        from axiom.extensions.builtins.publishing.engine import PublisherEngine

        engine = PublisherEngine()
        source = Path(params["source"])
        record = engine.publish(
            source,
            storage_override=params.get("storage"),
            draft=params.get("draft", False),
        )
        if record:
            return record.to_dict()
        return {"error": "Publishing blocked by branch policy or dirty state."}

    elif name == "signal_ingest":
        import argparse as _argparse

        from axiom.extensions.builtins.signals.cli import cmd_ingest

        source = params.get("source", "all")
        ingest_args = _argparse.Namespace(
            source=source, file=None, reprocess_from=None, correct=False
        )
        try:
            cmd_ingest(ingest_args)
            return {"message": f"Ingestion complete for source={source}.", "source": source}
        except Exception as e:
            return {"error": f"Ingestion failed: {e}", "source": source}

    elif name == "write_file":
        file_path = Path(params["file_path"])
        if not file_path.is_absolute():
            from axiom import REPO_ROOT

            file_path = REPO_ROOT / file_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(params["content"], encoding="utf-8")
        return {
            "message": f"Wrote {len(params['content'])} chars to {file_path}",
            "path": str(file_path),
        }

    elif name == "write_inbox_note":
        from datetime import datetime

        from axiom.extensions.builtins.signals.cli import INBOX_RAW

        ts = datetime.now(UTC).strftime("%Y-%m-%d_%H%M%S")
        INBOX_RAW.mkdir(parents=True, exist_ok=True)
        dest = INBOX_RAW / f"note_{ts}.md"
        dest.write_text(f"# Note — {ts}\n\n{params['text']}\n", encoding="utf-8")
        return {"message": f"Note saved as {dest.name}", "path": str(dest)}

    else:
        return {"error": f"Unknown tool: {name}"}
