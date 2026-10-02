# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tool-calling strategy — get tool calls out of *any* model, native or not.

Not every provider emits OpenAI ``tool_calls`` (e.g. a Qwen server without a
tool-call parser returns prose *about* the function). So agents resolve tool
calls through this layer, which is **native-first with a shim fallback**:

1. **native** — ask the provider for structured ``tool_calls`` (preferred).
2. **shim** — when native is unavailable/empty, instruct the model to reply with
   a single JSON action (``{"tool": …, "arguments": …}``) and parse that.

It is **self-monitoring** (so a hot patch can be issued without guessing):
- a provider *declared* native that returns no tool_calls → ``anomaly`` +
  warning ("server tool-calling likely misconfigured; falling back to shim"),
- a provider *pinned* to shim where native now works → ``anomaly`` + warning
  ("shim may be outdated/removable for <provider>"),
- neither path yields a call when one was clearly wanted → failure alert.

The per-provider mode (``native`` | ``shim`` | ``auto``) is **externalized** in
provider config and read per call, so it is hot-patchable without a restart.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("axiom.llm.tool_calling")

ToolMode = str  # "native" | "shim" | "auto"

_ALERT = "TOOL_CALLING_ALERT"  # grep-able log prefix for hot-patch alerting


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict


@dataclass
class ToolCallResult:
    calls: list[ToolCall] = field(default_factory=list)
    mode_used: str = "none"          # native | shim | none
    text: str = ""                   # assistant prose when no tool call
    anomaly: str | None = None       # set when native/shim health looks off


# --- shim protocol ------------------------------------------------------------

def shim_preamble(tools: list[dict]) -> str:
    """A system instruction teaching a non-tool-calling model to emit one JSON
    action. ``tools`` are OpenAI-format tool definitions."""
    lines = []
    for t in tools:
        fn = t.get("function", t)
        params = (fn.get("parameters") or {}).get("properties") or {}
        arglist = ", ".join(params) if params else "(none)"
        lines.append(f"- {fn['name']}: {fn.get('description', '')} | args: {arglist}")
    return (
        "You can use tools. To call a tool, reply with ONLY a single-line JSON object "
        "and nothing else:\n"
        '{"tool": "<tool_name>", "arguments": { ... }}\n'
        "Use double quotes, no markdown fences, no prose on that line.\n"
        "IMPORTANT: a tool returns *ground truth*. If the user asks about anything a tool "
        "can answer — what you're currently running, your task/job status or progress, "
        "stopping work, or verifying a measured value — you MUST call the tool. Never guess, "
        "assume, or answer such questions from memory. If no tool applies, answer in prose.\n"
        "Available tools:\n" + "\n".join(lines)
    )


_JSON_OBJ = re.compile(r"\{(?:[^{}]|\{[^{}]*\})*\}", re.DOTALL)


def parse_shim_response(text: str) -> ToolCall | None:
    """Extract a ``{"tool":…, "arguments":…}`` action from a model reply, even
    if wrapped in prose or ```json fences. Returns None for a plain answer."""
    if not text:
        return None
    for m in _JSON_OBJ.finditer(text):
        try:
            obj = json.loads(m.group(0))
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("tool"), str):
            args = obj.get("arguments") or obj.get("args") or {}
            if isinstance(args, dict):
                return ToolCall(name=obj["tool"], arguments=args)
    return None


# --- the resolver -------------------------------------------------------------

# native_fn() -> (calls, assistant_text); complete_fn(extra_system) -> text
NativeFn = Callable[[], "tuple[list[ToolCall], str]"]
CompleteFn = Callable[[str], str]


def resolve_tool_calls(
    tools: list[dict],
    *,
    native_fn: NativeFn,
    complete_fn: CompleteFn,
    mode: ToolMode = "auto",
    provider: str = "?",
    logger: logging.Logger | None = None,
) -> ToolCallResult:
    """Resolve tool calls native-first with a shim fallback, monitoring health.

    ``mode`` (from externalized per-provider config):
      - ``native`` — only native; if empty, no fallback (but alert if a call
        looked wanted).
      - ``shim``   — only shim; still probes native first to detect it now works.
      - ``auto``   — native first, shim fallback (default).
    """
    lg = logger or log
    anomaly: str | None = None

    # Fast path: a provider pinned to shim skips the (wasted) native probe.
    # Staleness ("native now works → shim removable") is detected out-of-band by
    # probe_native_support() on a health cadence, not on every turn's latency.
    if mode == "shim":
        try:
            shim_text = complete_fn(shim_preamble(tools))
        except Exception as exc:  # noqa: BLE001
            lg.warning("%s shim call failed for %s: %s", _ALERT, provider, exc)
            return ToolCallResult(mode_used="none")
        call = parse_shim_response(shim_text)
        if call is not None:
            return ToolCallResult(calls=[call], mode_used="shim")
        return ToolCallResult(mode_used="none", text=shim_text)

    native_calls: list[ToolCall] = []
    native_text = ""
    try:
        native_calls, native_text = native_fn()
    except Exception as exc:  # noqa: BLE001
        lg.warning("%s native call failed for %s: %s", _ALERT, provider, exc)
        native_text = ""

    if native_calls:
        return ToolCallResult(calls=native_calls, mode_used="native", text=native_text)

    if mode == "native":
        # Declared native but produced none. If the model's prose names a tool,
        # the server's tool-calling is likely misconfigured — alert + still
        # *try* the shim so the turn succeeds (resilience), flagged as anomaly.
        wanted = any((t.get("function", t)["name"]) in native_text for t in tools)
        if wanted:
            anomaly = (f"{provider} is declared native but returned no tool_calls while naming a tool "
                       "in prose — server tool-calling likely misconfigured; using shim this turn")
            lg.warning("%s %s", _ALERT, anomaly)
        else:
            return ToolCallResult(mode_used="none", text=native_text)

    # Shim path (mode auto/shim, or native-declared anomaly fallback).
    try:
        shim_text = complete_fn(shim_preamble(tools))
    except Exception as exc:  # noqa: BLE001
        lg.warning("%s shim call failed for %s: %s", _ALERT, provider, exc)
        return ToolCallResult(mode_used="none", text=native_text, anomaly=anomaly)

    call = parse_shim_response(shim_text)
    if call is not None:
        if mode == "auto" and not anomaly:
            # auto + native-empty + shim-worked is the expected state for a
            # genuinely non-tool-calling provider; not an anomaly by itself.
            pass
        return ToolCallResult(calls=[call], mode_used="shim", text="", anomaly=anomaly)

    # Neither produced a call — a plain answer (shim_text) is the normal case.
    return ToolCallResult(mode_used="none", text=shim_text or native_text, anomaly=anomaly)


def probe_native_support(
    native_fn: NativeFn,
    *,
    provider: str = "?",
    logger: logging.Logger | None = None,
) -> bool:
    """Out-of-band staleness check: does the provider emit native tool_calls now?

    Run on a health cadence (not per turn). Returns True if native works — and if
    the provider is currently pinned to ``shim``, that's the signal the pin is
    outdated; this logs an alert so a hot config flip (shim→auto/native) can be
    issued without a restart."""
    lg = logger or log
    try:
        calls, _ = native_fn()
    except Exception as exc:  # noqa: BLE001
        lg.warning("%s native probe failed for %s: %s", _ALERT, provider, exc)
        return False
    if calls:
        lg.warning("%s native tool-calls now work for %s — a shim pin is outdated; "
                   "flip tool_mode to 'auto' or 'native'.", _ALERT, provider)
        return True
    return False


__all__ = [
    "ToolCall",
    "ToolCallResult",
    "ToolMode",
    "shim_preamble",
    "parse_shim_response",
    "resolve_tool_calls",
    "probe_native_support",
]
