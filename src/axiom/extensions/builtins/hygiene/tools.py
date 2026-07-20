# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TIDY Agent tools — tool definitions and execute() dispatcher.

Follows the Doctor Agent pattern (extensions/builtins/diagnostics/tools.py):
OpenAI function-calling format defs + execute(name, params) dispatcher.

Tools are divided into READ (safe) and WRITE (mutating) categories.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

# --- Tool definitions (OpenAI function-calling format) ---

TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "query_vitals",
            "description": (
                "Get the current VitalsSnapshot as JSON. Includes disk usage, "
                "memory, active scratch entries, and network stats."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_entries",
            "description": (
                "List all scratch manifest entries, optionally filtered by "
                "owner or retention policy."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {
                        "type": "string",
                        "description": "Filter by owner name (e.g. 'chat.mermaid').",
                    },
                    "retention": {
                        "type": "string",
                        "description": "Filter by retention policy ('transient', 'session', 'hour', 'day').",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_disk",
            "description": "Check disk free/total for the scratch base directory.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_process_memory",
            "description": "Get RSS and heap info for the current process. Requires psutil.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_owner_history",
            "description": (
                "Get acquisition/release history for a specific owner from "
                "the vitals history buffer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {
                        "type": "string",
                        "description": "Owner name to query history for.",
                    },
                },
                "required": ["owner"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "release_entries",
            "description": (
                "Force-release scratch entries by owner, retention, or minimum age. "
                "This is a WRITE operation that deletes files/directories."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {
                        "type": "string",
                        "description": "Release entries matching this owner.",
                    },
                    "retention": {
                        "type": "string",
                        "description": "Release entries matching this retention policy.",
                    },
                    "min_age_seconds": {
                        "type": "integer",
                        "description": "Only release entries older than this many seconds.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "adjust_threshold",
            "description": (
                "Temporarily adjust a vitals threshold for the current session. "
                "Valid keys: disk_pct_warn, disk_pct_crit, mem_pct_warn, mem_pct_crit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Threshold key to adjust.",
                    },
                    "value": {
                        "type": "number",
                        "description": "New threshold value.",
                    },
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate",
            "description": (
                "Publish an escalation event to the bus for human attention. "
                "Use when automated remediation is insufficient."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why escalation is needed.",
                    },
                    "diagnosis": {
                        "type": "string",
                        "description": "Detailed diagnosis of the issue.",
                    },
                    "recommendations": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Recommended actions for the human.",
                    },
                },
                "required": ["reason"],
            },
        },
    },
]


# --- Execution context (set by MoAgent before tool loop) ---

_context: dict[str, Any] = {}


def set_context(
    mgr: Any,
    monitor: Any | None = None,
    bus: Any | None = None,
) -> None:
    """Set the execution context for tool handlers."""
    _context["mgr"] = mgr
    _context["monitor"] = monitor
    _context["bus"] = bus


def execute(name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a tool call to the appropriate handler."""
    handlers = {
        "query_vitals": _exec_query_vitals,
        "list_entries": _exec_list_entries,
        "check_disk": _exec_check_disk,
        "check_process_memory": _exec_check_process_memory,
        "read_owner_history": _exec_read_owner_history,
        "release_entries": _exec_release_entries,
        "adjust_threshold": _exec_adjust_threshold,
        "escalate": _exec_escalate,
    }
    handler = handlers.get(name)
    if not handler:
        return {"error": f"Unknown tool: {name}"}
    try:
        return handler(params)
    except Exception as e:
        return {"error": f"{name} failed: {e}"}


# --- Handlers ---

def _exec_query_vitals(params: dict[str, Any]) -> dict[str, Any]:
    monitor = _context.get("monitor")
    if monitor is None:
        return {"error": "VitalsMonitor not available"}
    snap = monitor.sample()
    return snap.to_dict()


def _exec_list_entries(params: dict[str, Any]) -> dict[str, Any]:
    mgr = _context.get("mgr")
    if mgr is None:
        return {"error": "MoManager not available"}

    entries = mgr.all_entries()
    owner_filter = params.get("owner")
    retention_filter = params.get("retention")

    if owner_filter:
        entries = [e for e in entries if e.owner == owner_filter]
    if retention_filter:
        entries = [e for e in entries if e.retention == retention_filter]

    return {
        "entries": [e.to_dict() for e in entries],
        "count": len(entries),
    }


def _exec_check_disk(params: dict[str, Any]) -> dict[str, Any]:
    import shutil
    mgr = _context.get("mgr")
    if mgr is None:
        return {"error": "MoManager not available"}
    try:
        usage = shutil.disk_usage(mgr.base_dir)
        return {
            "path": str(mgr.base_dir),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_pct": round(usage.used / usage.total * 100, 1) if usage.total else 0,
        }
    except OSError as e:
        return {"error": f"disk_usage failed: {e}"}


def _exec_check_process_memory(params: dict[str, Any]) -> dict[str, Any]:
    import os
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        mem = proc.memory_info()
        return {
            "rss_bytes": mem.rss,
            "vms_bytes": mem.vms,
            "rss_mb": round(mem.rss / 1024 / 1024, 1),
            "system_mem_pct": psutil.virtual_memory().percent,
        }
    except ImportError:
        return {"error": "psutil not installed — memory inspection unavailable"}
    except Exception as e:
        return {"error": f"memory check failed: {e}"}


def _exec_read_owner_history(params: dict[str, Any]) -> dict[str, Any]:
    owner = params.get("owner", "")
    if not owner:
        return {"error": "owner is required"}

    monitor = _context.get("monitor")
    if monitor is None:
        return {"error": "VitalsMonitor not available"}

    history = []
    for snap in monitor.history:
        count = snap.entries_by_owner.get(owner, 0)
        bytes_used = snap.bytes_by_owner.get(owner, 0)
        history.append({
            "timestamp": snap.timestamp,
            "entries": count,
            "bytes": bytes_used,
        })

    return {"owner": owner, "history": history}


def _exec_release_entries(params: dict[str, Any]) -> dict[str, Any]:
    mgr = _context.get("mgr")
    if mgr is None:
        return {"error": "MoManager not available"}

    from datetime import datetime

    entries = mgr.all_entries()
    owner = params.get("owner")
    retention = params.get("retention")
    min_age = params.get("min_age_seconds", 0)
    now = datetime.now(UTC)

    released = 0
    freed_bytes = 0

    for e in entries:
        if owner and e.owner != owner:
            continue
        if retention and e.retention != retention:
            continue
        if min_age:
            try:
                created = datetime.fromisoformat(e.created_at)
                age = (now - created).total_seconds()
                if age < min_age:
                    continue
            except (ValueError, TypeError):
                continue

        from pathlib import Path
        freed_bytes += e.size_bytes
        mgr.release(Path(e.path))
        released += 1

    return {
        "released": released,
        "freed_bytes": freed_bytes,
    }


def _exec_adjust_threshold(params: dict[str, Any]) -> dict[str, Any]:
    key = params.get("key", "")
    value = params.get("value")
    if not key or value is None:
        return {"error": "key and value are required"}

    monitor = _context.get("monitor")
    if monitor is None:
        return {"error": "VitalsMonitor not available"}

    valid_keys = {
        "disk_pct_warn", "disk_pct_crit",
        "mem_pct_warn", "mem_pct_crit",
    }
    if key not in valid_keys:
        return {"error": f"Invalid key '{key}'. Valid: {sorted(valid_keys)}"}

    old_value = getattr(monitor.thresholds, key)
    setattr(monitor.thresholds, key, float(value))
    return {
        "key": key,
        "old_value": old_value,
        "new_value": float(value),
    }


def _exec_escalate(params: dict[str, Any]) -> dict[str, Any]:
    reason = params.get("reason", "")
    diagnosis = params.get("diagnosis", "")
    recommendations = params.get("recommendations", [])

    bus = _context.get("bus")
    if bus is not None:
        bus.publish("tidy.escalation", {
            "reason": reason,
            "diagnosis": diagnosis,
            "recommendations": recommendations,
        }, source="tidy.agent")

    return {
        "escalated": True,
        "reason": reason,
        "recommendations": recommendations,
    }
