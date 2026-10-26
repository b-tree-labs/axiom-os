# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Other harnesses' sessions in their real on-disk formats, under a temp home, for the /renew source tests.

Formats are the ones the readers document and the existing parsers' fixtures use: Claude Code project JSONL
(``~/.claude/projects/<slug>/<uuid>.jsonl``) and Codex rollouts (``~/.codex/sessions/Y/M/D/rollout-*.jsonl``).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: Built from pieces so no secret scanner trips on the test file itself.
SECRET = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1"

CLAUDE_ID = "c1a0de00-0000-4000-8000-000000000001"
CLAUDE_OLD_ID = "c1a0de00-0000-4000-8000-000000000002"
CODEX_ID = "019e04f7-a2eb-7790-b5f3-bd20fd63b58c"
CODEX_ARCHIVED_ID = "019e04f7-0000-7790-b5f3-000000000099"


def _write(path: Path, records: list[dict], hours_ago: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    stamp = (datetime.now(UTC) - timedelta(hours=hours_ago)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def claude_session(home: Path, sid: str = CLAUDE_ID, *, first_prompt: str = "Plan the detector calibration", hours_ago=3.0,
                   title: str = "Detector calibration plan", extra_tool_arg: str = "ls -la") -> Path:
    base = {"sessionId": sid, "cwd": "/work/detector", "gitBranch": "main", "version": "2.1"}
    records = [
        {"type": "ai-title", "aiTitle": title, "sessionId": sid},
        {"type": "permission-mode", "permissionMode": "default"},
        {**base, "type": "user", "uuid": "u1", "timestamp": "2026-10-01T10:00:00Z",
         "message": {"role": "user", "content": first_prompt}},
        {**base, "type": "assistant", "uuid": "a1", "timestamp": "2026-10-01T10:00:05Z",
         "message": {"role": "assistant", "model": "m", "content": [
             {"type": "thinking", "thinking": "hmm"},
             {"type": "text", "text": "I will look at the files."},
             {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": extra_tool_arg}},
         ]}},
        {**base, "type": "user", "uuid": "u2", "timestamp": "2026-10-01T10:00:06Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 120}]}},
        {**base, "type": "assistant", "uuid": "a2", "timestamp": "2026-10-01T10:00:09Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "Calibration needs two reference runs."}]}},
        {**base, "type": "assistant", "uuid": "side", "isSidechain": True, "timestamp": "2026-10-01T10:00:10Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "SUBAGENT CHATTER"}]}},
        {**base, "type": "user", "uuid": "meta", "isMeta": True, "timestamp": "2026-10-01T10:00:11Z",
         "message": {"role": "user", "content": "META NOISE"}},
        {**base, "type": "user", "uuid": "u3", "timestamp": "2026-10-01T10:01:00Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "Thanks, and the second run?"}]}},
        {**base, "type": "assistant", "uuid": "a3", "timestamp": "2026-10-01T10:01:05Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "Run it after warm-up."}]}},
    ]
    return _write(home / ".claude" / "projects" / "-work-detector" / f"{sid}.jsonl", records, hours_ago)


def codex_session(
    home: Path, sid: str = CODEX_ID, *, hours_ago=1.0, archived=False, prompt="Check the flux log", command="cat flux.log"
) -> Path:
    meta = {"id": sid, "timestamp": "2026-10-02T09:00:00.000Z", "cwd": "/work/flux", "cli_version": "0.129.0"}

    def item(payload, ts="2026-10-02T09:00:01.000Z"):
        return {"timestamp": ts, "type": "response_item", "payload": payload}

    records = [
        {"timestamp": "2026-10-02T09:00:00.000Z", "type": "session_meta", "payload": meta},
        {"timestamp": "2026-10-02T09:00:00.500Z", "type": "event_msg", "payload": {"type": "task_started"}},
        item({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "<permissions>..."}]}),
        item({"type": "message", "role": "user",
              "content": [{"type": "input_text", "text": "<environment_context>cwd=/work/flux</environment_context>"}]}),
        item({"type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]}),
        item({"type": "reasoning", "summary": []}),
        item({"type": "function_call", "name": "shell", "arguments": json.dumps({"command": command}),
              "call_id": "c1"}),
        item({"type": "function_call_output", "call_id": "c1", "output": "y" * 50}),
        item({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "The flux is flat."}]}),
        item({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Plot it."}]}),
        item({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done."}]}),
    ]
    root = home / ".codex" / ("archived_sessions" if archived else "sessions/2026/10/02")
    return _write(root / f"rollout-2026-10-02T09-00-00-{sid}.jsonl", records, hours_ago)


def fingerprint(root: Path) -> dict[str, tuple[int, str]]:
    """Every file under ``root``: (mtime_ns, sha256). Equal before and after means the source was not touched."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p)] = (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
    return out
