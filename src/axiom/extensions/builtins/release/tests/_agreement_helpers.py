# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Builders for the agreement tests, made from the real transcript shapes.

``fixtures/agreement/real_shapes.jsonl`` holds one anonymised row of every kind a
Claude Code session transcript showed on the owner's machine (typed prompt,
queued prompt, assistant text, tool result, a message from another session, a
task notice, a compaction summary, a slash command...). The builders copy those
rows and change only the text, the time and the ids, so a test sees the real
field set and not a guess at it.
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "agreement" / "real_shapes.jsonl"
T0 = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
REPO = "owner/name"
HEAD = "a" * 40


def real_rows() -> dict[str, dict]:
    rows = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line]
    return {row["_label"]: row for row in rows}


def _stamp(row: dict, *, session: str, at: datetime) -> dict:
    row = copy.deepcopy(row)
    row.pop("_label", None)
    row["uuid"] = str(uuid.uuid4())
    row["promptId"] = str(uuid.uuid4())
    row["sessionId"] = session
    if "session_id" in row:
        row["session_id"] = session
    row["timestamp"] = at.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return row


def _set_text(row: dict, text: str) -> None:
    content = row["message"]["content"]
    if isinstance(content, str):
        row["message"]["content"] = text
        return
    for block in content:
        if block.get("type") in {"text", "tool_result"}:
            key = "text" if block["type"] == "text" else "content"
            block[key] = text
            return


def human(text: str, at: datetime, session: str = "S1") -> dict:
    row = _stamp(real_rows()["human_typed"], session=session, at=at)
    _set_text(row, text)
    return row


def assistant(text: str, at: datetime, session: str = "S1") -> dict:
    row = _stamp(real_rows()["assistant_text"], session=session, at=at)
    row["message"]["content"] = [{"type": "text", "text": text}]
    return row


def assistant_tool(at: datetime, session: str = "S1", command: str = "ls") -> dict:
    row = _stamp(real_rows()["assistant_tool"], session=session, at=at)
    row["message"]["content"][0]["input"] = {"command": command, "description": "run"}
    return row


def tool_result(text: str, at: datetime, session: str = "S1") -> dict:
    row = _stamp(real_rows()["tool_result"], session=session, at=at)
    _set_text(row, text)
    return row


def peer(text: str, at: datetime, session: str = "S1") -> dict:
    row = _stamp(real_rows()["peer"], session=session, at=at)
    body = f'Another Claude session sent a message:\n<cross-session-message from="uds:/tmp/x.sock" from-name="peer" from-mode="bypass">\n{text}\n</cross-session-message>'
    _set_text(row, body)
    row["origin"]["body"] = text
    return row


def task_note(text: str, at: datetime, session: str = "S1") -> dict:
    row = _stamp(real_rows()["task_note"], session=session, at=at)
    _set_text(row, f"<task-notification>\n<summary>{text}</summary>\n</task-notification>")
    return row


def compact(text: str, at: datetime, session: str = "S1") -> dict:
    row = _stamp(real_rows()["compact"], session=session, at=at)
    _set_text(row, text)
    return row


def write_transcript(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def hook_payload(row: dict, transcript: Path) -> dict:
    """What UserPromptSubmit gives a hook for one transcript row (documented fields only)."""
    content = row["message"]["content"]
    if isinstance(content, str):
        text = content
    else:
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        text = text or "".join(
            str(b.get("content", "")) for b in content if b.get("type") == "tool_result"
        )
    return {
        "session_id": row["sessionId"],
        "prompt_id": row["promptId"],
        "transcript_path": str(transcript),
        "cwd": "/Users/example/work",
        "permission_mode": "bypassPermissions",
        "hook_event_name": "UserPromptSubmit",
        "prompt": text,
    }


def minutes(n: float) -> timedelta:
    return timedelta(minutes=n)
