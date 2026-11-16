# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Recover tool calls a model wrote as prose instead of as tool_use blocks.

Native tool-use is offered to every provider. Not every provider honours it:
an OpenAI-compatible endpoint may accept a ``tools`` parameter and ignore it,
leaving the model to improvise the protocol from the system prompt. It then
writes something that looks like a call — a fenced block, a bracket line — and
the turn ends with no tool calls and a paragraph that reads like an answer.

Observed on a real node: a question about pulling a week of telemetry came back
with a fenced ``telemetry_aggregate`` block naming a metric that does not exist
and an argument that is not in the signature. Nothing ran. The reader has no
way to tell that from a result.

Two jobs here, and the split matters:

``recover_tool_calls`` returns what can actually be run. It validates every
candidate against the live tool table, so the registry decides what executes and
the model's text never does. That also makes this work for every extension for
free — a tool is recoverable the moment it is registered, with no per-extension
wiring, including tools a site ships in its own extension.

``looks_like_a_tool_attempt`` answers the separate question of whether the model
was *trying* to call something, even when nothing is recoverable — an unknown
name, malformed JSON. That case must not be printed as an answer either, and
the caller needs to know the difference between "the model answered" and "the
model tried to act and we did not notice".
"""

from __future__ import annotations

import json
import re
from typing import Any

#: Fenced blocks a model might use for a call. ```json is included because
#: models reach for it as readily as ```tool; the payload shape decides.
_FENCE = re.compile(
    r"```(?:tool|tool_call|tool_use|json|)\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)

_BRACKET = re.compile(r"^\[tool:\s*([^\]]+)\]\s*(.*)$")

#: Keys a model uses for the arguments of a call, in the order we trust them.
_ARG_KEYS = ("arguments", "parameters", "input", "args", "params")


def _candidates(text: str) -> list[tuple[str, dict[str, Any] | None]]:
    """Every (name, arguments) pair the text appears to propose.

    ``arguments`` is None when the payload named a tool but could not be
    parsed — still an attempt, just not a runnable one.
    """
    found: list[tuple[str, dict[str, Any] | None]] = []

    for line in text.splitlines():
        match = _BRACKET.match(line.strip())
        if not match:
            continue
        name = match.group(1).strip()
        rest = match.group(2).strip()
        try:
            found.append((name, json.loads(rest) if rest else {}))
        except json.JSONDecodeError:
            found.append((name, None))

    for block in _FENCE.findall(text):
        payload = block.strip()
        if not payload:
            continue
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            # A fence that names a tool but will not parse is an attempt we
            # cannot run. Recovering the name is enough to refuse honestly.
            name = _name_from_broken_payload(payload)
            if name:
                found.append((name, None))
            continue
        if not isinstance(parsed, dict):
            continue
        name = parsed.get("name")
        if not isinstance(name, str) or not name:
            # A plain JSON payload with no "name" is someone being shown data,
            # not a call. This is what keeps example output as prose.
            continue
        args: dict[str, Any] | None = None
        for key in _ARG_KEYS:
            value = parsed.get(key)
            if isinstance(value, dict):
                args = value
                break
        found.append((name, args if args is not None else {}))

    return found


def _name_from_broken_payload(payload: str) -> str | None:
    match = re.search(r'"name"\s*:\s*"([^"]+)"', payload)
    return match.group(1) if match else None


def recover_tool_calls(text: str, all_tools: dict[str, Any]) -> list[Any]:
    """Runnable tool calls found in ``text``, validated against ``all_tools``.

    Only names present in the registry are returned. The text is model output;
    it does not get to decide what runs.
    """
    if not text:
        return []

    from axiom.infra.gateway import ToolUseBlock

    blocks = []
    for index, (name, args) in enumerate(_candidates(text)):
        if args is None or name not in all_tools:
            continue
        blocks.append(
            ToolUseBlock(tool_id=f"recovered_{index}_{name}", name=name, input=args)
        )
    return blocks


def looks_like_a_tool_attempt(text: str, all_tools: dict[str, Any]) -> bool:
    """Whether the model was trying to call something, runnable or not.

    True for a malformed payload or an invented tool name — the cases where
    nothing can be recovered and the text still must not be served as an
    answer, because it describes an action that did not happen.
    """
    if not text:
        return False
    return bool(_candidates(text))


__all__ = ["looks_like_a_tool_attempt", "recover_tool_calls"]
