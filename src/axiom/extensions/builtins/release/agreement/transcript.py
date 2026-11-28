# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Read a Claude Code session transcript and say what each row is.

Verified against real transcripts on the owner's machine (July to November 2026).
A transcript is JSONL. The rows that matter are ``user`` and ``assistant``. A
``user`` row is not always a person:

* typed by a person: ``origin.kind == "human"`` (``promptSource`` is ``typed``,
  ``queued`` or ``suggestion_accepted``), not ``isMeta``, no ``toolUseResult``;
* a tool result: a ``user`` row whose content is a ``tool_result`` block, with a
  ``toolUseResult`` field;
* another session's message: ``origin.kind == "peer"``, ``isMeta`` true, and the
  text opens with ``Another Claude session sent a message:`` and a
  ``<cross-session-message>`` wrapper;
* a background task notice: ``origin.kind == "task-notification"``;
* a compaction summary: no ``origin``, ``isCompactSummary`` true, or text that
  opens "This session is being continued";
* a slash command (``<command-name>``), a local-command caveat (``isMeta``), an
  automatic continuation (``origin.kind == "auto-continuation"``), a scheduled
  wake-up (``wakeupSource``).

A row with no ``origin`` is never the owner: the harness did not say a person
typed it. That is the fail-closed choice, and the cost is small (every row from
July to November that a person typed has ``origin.kind == "human"``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

OWNER = "owner"
COMMAND = "command"
PEER = "peer"
TOOL_RESULT = "tool_result"
TASK_NOTIFICATION = "task_notification"
SUMMARY = "summary"
SYSTEM = "system"
ASSISTANT = "assistant"
OTHER = "other"

_PEER_OPENING = "Another Claude session sent a message"
_SUMMARY_OPENING = "This session is being continued"


@dataclass(frozen=True)
class Turn:
    index: int
    kind: str
    session_id: str
    prompt_id: str
    time: datetime | None
    text: str
    #: Everything searchable in the row, including tool inputs, for "was this pull request mentioned".
    searchable: str = ""
    raw: dict = field(default_factory=dict, compare=False, repr=False)


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _blocks(content: Any) -> list[dict]:
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def message_text(row: dict) -> str:
    """The words in a row: a string, or the text blocks of a list. Images and tool blocks add none."""
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in _blocks(content) if b.get("type") == "text")


def _searchable(row: dict) -> str:
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in _blocks(content):
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind == "tool_use":
            parts.append(json.dumps(block.get("input", ""), sort_keys=True))
        elif kind == "tool_result":
            inner = block.get("content")
            parts.append(inner if isinstance(inner, str) else json.dumps(inner, sort_keys=True))
    return "\n".join(parts)


def classify(row: dict) -> str:
    """What one transcript row is. Only ``OWNER`` can carry the owner's words."""
    kind = row.get("type")
    if kind == "assistant":
        return ASSISTANT
    if kind != "user":
        return OTHER
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    blocks = _blocks(content)
    if row.get("toolUseResult") is not None or any(b.get("type") == "tool_result" for b in blocks):
        return TOOL_RESULT
    origin = row.get("origin")
    origin_kind = origin.get("kind") if isinstance(origin, dict) else None
    text = message_text(row)
    stripped = text.lstrip()
    if (
        origin_kind == "peer"
        or stripped.startswith(_PEER_OPENING)
        or "<cross-session-message" in stripped[:400]
    ):
        return PEER
    if origin_kind == "task-notification" or stripped.startswith("<task-notification"):
        return TASK_NOTIFICATION
    if row.get("isCompactSummary") or stripped.startswith(_SUMMARY_OPENING):
        return SUMMARY
    if row.get("isSidechain"):
        return OTHER
    if row.get("wakeupSource") or row.get("turnOrigin") in {
        "scheduled",
        "peer",
        "task_notification",
        "auto_continuation",
    }:
        return COMMAND if stripped.startswith("<command-") else SYSTEM
    if row.get("isMeta") or origin_kind in {"auto-continuation"}:
        return SYSTEM
    if origin_kind != "human":
        # No origin, or one we do not know: the harness did not say a person typed it.
        return COMMAND if stripped.startswith("<command-") else OTHER
    if stripped.startswith("<command-") or stripped.startswith("<local-command"):
        return COMMAND
    return OWNER


def read_transcript(path: Path | str) -> list[Turn]:
    """Every ``user`` and ``assistant`` row, in file order. Raises ``OSError`` if unreadable."""
    turns: list[Turn] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("type") not in {"user", "assistant"}:
                continue
            turns.append(
                Turn(
                    index=index,
                    kind=classify(row),
                    session_id=str(row.get("sessionId") or row.get("session_id") or ""),
                    prompt_id=str(row.get("promptId") or ""),
                    time=parse_time(row.get("timestamp")),
                    text=message_text(row),
                    searchable=_searchable(row),
                    raw=row,
                )
            )
    return turns
