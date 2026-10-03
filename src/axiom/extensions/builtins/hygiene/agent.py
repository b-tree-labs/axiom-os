# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""MoAgent — Layer 3 LLM-powered diagnosis, procurement, and escalation.

Follows the DoctorAgent pattern (extensions/builtins/diagnostics/agent.py):
structured signal in, multi-turn tool loop via gateway.complete_with_tools(),
actionable result out.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .tools import TOOL_DEFS, execute, set_context
from .vitals import LeakSignal


@dataclass
class MoVerdict:
    """Result of an TIDY diagnosis session."""

    level: str = "monitoring"  # "resolved" | "escalated" | "monitoring"
    diagnosis: str = ""
    actions_taken: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    leak_signals: list[LeakSignal] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["leak_signals"] = [asdict(ls) for ls in self.leak_signals]
        return d


@dataclass
class ProcureResult:
    """Result of a space procurement attempt."""

    success: bool = False
    freed_bytes: int = 0
    available_bytes: int = 0
    entries_released: int = 0
    escalated: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_MO_SYSTEM_PROMPT = """\
You are TIDY (Micro-Obliterator), the autonomous resource steward for Neutron OS.
Named after the obsessive cleaning robot from AXI, your job is to keep the
system's scratch space, disk, memory, and network healthy.

## Your Capabilities

You monitor and manage:
- Scratch space: temporary files and directories used by all Neut subsystems
- Disk usage: detect pressure, identify what's consuming space
- Memory: track RSS and system memory (when psutil is available)
- Network: detect latency spikes, error bursts, bandwidth surges
- Leaks: identify subsystems that accumulate resources without releasing them

## Your Tools

READ tools (safe, no side effects):
- query_vitals: Current snapshot of all system metrics
- list_entries: Browse the scratch manifest
- check_disk: Disk free/total for scratch directory
- check_process_memory: Process RSS and system memory
- read_owner_history: Track a specific owner's resource usage over time

WRITE tools (mutating — use judiciously):
- release_entries: Force-release scratch entries (deletes files)
- adjust_threshold: Temporarily change monitoring thresholds
- escalate: Alert humans when automated fixes are insufficient

## Guidelines

1. Always query_vitals first to understand the current state.
2. Diagnose before acting — identify the root cause, not just symptoms.
3. Prefer releasing expired/low-priority entries before escalating.
4. If a leak persists across multiple cycles, escalate — don't mask it.
5. Be concise in your diagnosis. Operators are busy.
6. When procuring space, release in priority order:
   - Expired retention entries (hour/day past their time)
   - Transient entries from dead processes
   - Oldest low-priority entries
   - Never release active session entries without escalation

## Current Context

{context}
"""


class MoAgent:
    """LLM-powered resource diagnosis and management agent."""

    MAX_ROUNDS = 4

    def __init__(self, gateway: Any, bus: Any | None = None):
        self.gateway = gateway
        self.bus = bus
        self._mgr = None
        self._monitor = None

    def set_manager(self, mgr: Any, monitor: Any | None = None) -> None:
        """Wire up the manager and monitor for tool execution."""
        self._mgr = mgr
        self._monitor = monitor
        set_context(mgr, monitor, self.bus)

    def diagnose(self, signal: dict[str, Any]) -> MoVerdict:
        """Diagnose an anomaly signal from Layer 2."""
        if self._mgr is None:
            return MoVerdict(level="monitoring", diagnosis="Manager not configured")

        set_context(self._mgr, self._monitor, self.bus)

        # Layer 1 — TIDY persona prepended to identity. Best-effort.
        _persona_text = ""
        try:
            from pathlib import Path as _Path

            from axiom.agents.persona_loader import load_agent_persona

            _persona_dir = _Path(__file__).parent / "agents" / "tidy"
            _persona_text = load_agent_persona(_persona_dir) or ""
        except Exception:
            pass

        _operational = _MO_SYSTEM_PROMPT.format(
            context=f"Signal type: {signal.get('type', 'unknown')}\n"
                    f"Pressure level: {signal.get('level', 'unknown')}"
        )
        system = (_persona_text + "\n\n" + _operational) if _persona_text else _operational
        messages = [{
            "role": "user",
            "content": (
                f"## Anomaly Signal\n\n"
                f"```json\n{json.dumps(signal, indent=2)}\n```\n\n"
                f"Diagnose this issue, attempt to resolve it if possible, "
                f"and escalate if automated remediation is insufficient."
            ),
        }]

        diagnosis = ""
        actions: list[str] = []

        for _round in range(self.MAX_ROUNDS):
            response = self.gateway.complete_with_tools(
                messages=messages,
                system=system,
                tools=TOOL_DEFS,
                max_tokens=2048,
                task="tidy",
            )

            if not response.success:
                return MoVerdict(
                    level="monitoring",
                    diagnosis=f"LLM call failed: {response.error}",
                )

            if not response.tool_use:
                diagnosis = response.text
                break

            if response.text:
                diagnosis += response.text + "\n"

            tool_results = self._process_tools(response, actions)
            messages.append(self._assistant_message(response))
            for tool_id, name, result in tool_results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": name,
                    "content": json.dumps(result),
                })

        # Check if escalation happened
        escalated = any("escalate" in a for a in actions)
        level = "escalated" if escalated else ("resolved" if actions else "monitoring")

        verdict = MoVerdict(
            level=level,
            diagnosis=diagnosis.strip(),
            actions_taken=actions,
        )

        # Publish result
        if self.bus:
            topic = "tidy.resolved" if level == "resolved" else "tidy.advisory"
            self.bus.publish(topic, verdict.to_dict(), source="tidy.agent")

        return verdict

    def procure(self, requested_bytes: int, requester: str) -> ProcureResult:
        """Attempt to free space for a requester."""
        if self._mgr is None:
            return ProcureResult(message="Manager not configured")

        import shutil
        # Quick check: is space already available?
        try:
            usage = shutil.disk_usage(self._mgr.base_dir)
            if usage.free >= requested_bytes:
                return ProcureResult(
                    success=True,
                    available_bytes=usage.free,
                    message=f"Sufficient space available ({self._fmt(usage.free)} free)",
                )
        except OSError:
            pass

        # Run a sweep first
        sweep_result = self._mgr.sweep()

        # Check again after sweep
        usage = None
        try:
            usage = shutil.disk_usage(self._mgr.base_dir)
            if usage.free >= requested_bytes:
                return ProcureResult(
                    success=True,
                    freed_bytes=0,
                    available_bytes=usage.free,
                    entries_released=sweep_result.get("expired", 0) + sweep_result.get("orphaned", 0),
                    message=f"Freed space via sweep ({self._fmt(usage.free)} now free)",
                )
        except OSError:
            pass

        # If LLM available, use it to reason about what else to release
        if self.gateway and self.gateway.available:
            set_context(self._mgr, self._monitor, self.bus)
            available_str = self._fmt(usage.free) if usage else "unknown"
            system = _MO_SYSTEM_PROMPT.format(
                context=f"Procurement request: {requester} needs {self._fmt(requested_bytes)}"
            )
            messages = [{
                "role": "user",
                "content": (
                    f"Requester '{requester}' needs {self._fmt(requested_bytes)} of scratch space "
                    f"but only {available_str} is available after sweep. "
                    f"Use list_entries and release_entries to free up space. "
                    f"Prioritize expired and low-value entries. "
                    f"If you cannot free enough, escalate."
                ),
            }]

            actions: list[str] = []
            for _round in range(self.MAX_ROUNDS):
                response = self.gateway.complete_with_tools(
                    messages=messages,
                    system=system,
                    tools=TOOL_DEFS,
                    max_tokens=2048,
                    task="tidy",
                )
                if not response.success or not response.tool_use:
                    break

                tool_results = self._process_tools(response, actions)
                messages.append(self._assistant_message(response))
                for tool_id, name, result in tool_results:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "name": name,
                        "content": json.dumps(result),
                    })

            # Final check
            try:
                usage = shutil.disk_usage(self._mgr.base_dir)
                if usage.free >= requested_bytes:
                    return ProcureResult(
                        success=True,
                        available_bytes=usage.free,
                        entries_released=len([a for a in actions if "release" in a]),
                        message=f"Procured space for {requester} ({self._fmt(usage.free)} free)",
                    )
            except OSError:
                pass

        # Escalate: couldn't free enough
        if self.bus:
            self.bus.publish("tidy.escalation", {
                "reason": f"Cannot procure {self._fmt(requested_bytes)} for {requester}",
                "available": usage.free if usage else 0,
            }, source="tidy.agent")

        return ProcureResult(
            success=False,
            available_bytes=usage.free if usage else 0,
            escalated=True,
            message=f"Disk full — escalated. {requester} needs {self._fmt(requested_bytes)}",
        )

    def advise(self, question: str, context: dict[str, Any] | None = None) -> str:
        """Answer a question from another agent about resources."""
        if not self.gateway or not self.gateway.available:
            return "TIDY advisory unavailable (no LLM configured)"

        if self._mgr is not None:
            status = self._mgr.status()
        else:
            status = {}

        system = _MO_SYSTEM_PROMPT.format(
            context=f"Advisory query from another agent.\n"
                    f"Current status: {json.dumps(status, indent=2)}"
        )
        messages = [{
            "role": "user",
            "content": f"{question}\n\nAdditional context: {json.dumps(context or {})}",
        }]

        response = self.gateway.complete_with_tools(
            messages=messages,
            system=system,
            tools=[],  # No tools for simple advisory
            max_tokens=1024,
            task="tidy",
        )

        return response.text if response.success else "TIDY advisory failed"

    # --- Internal ---

    def _process_tools(
        self, response: Any, actions: list[str],
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """Execute tool calls and track actions."""
        results = []
        for tool_block in response.tool_use:
            result = execute(tool_block.name, tool_block.input)
            actions.append(f"{tool_block.name}({json.dumps(tool_block.input)})")
            results.append((tool_block.tool_id, tool_block.name, result))
        return results

    def _assistant_message(self, response: Any) -> dict:
        """Build the assistant message with tool_calls for the next round."""
        msg: dict[str, Any] = {"role": "assistant", "content": response.text or ""}
        if response.tool_use:
            msg["tool_calls"] = [
                {
                    "id": t.tool_id,
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "arguments": json.dumps(t.input),
                    },
                }
                for t in response.tool_use
            ]
        return msg

    @staticmethod
    def _fmt(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if abs(n) < 1024:
                return f"{n:.1f} {unit}"
            n = int(n / 1024)
        return f"{n:.1f} TB"
