# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The prompt log the hook writes and the agreement records that cite it.

Both are append-only JSONL files under the user's state folder
(``<state>/agents/rivet/agreement/``), mode 0600, never inside a repository.

Each prompt line carries ``prev``, a hash of the line before it, so an edit or a
deleted line in the middle is visible. That is tamper evidence, not tamper proof:
a process that can append to the file can also compute the next hash, and one that
can write the owner's files can also write a transcript. The log's value is that the
harness, not the model, writes it, and that the verifier cross-checks it with the
transcript the harness writes separately. ADR-291 states that limit plainly.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import paths
from .transcript import parse_time

#: Longer prompts are kept to this many characters and marked truncated (a paste is not a yes).
MAX_PROMPT_CHARS = 262_144
_DEDUPE_TAIL_BYTES = 65_536

_WRAPPED_OPENINGS = (
    "Another Claude session sent a message",
    "<cross-session-message",
    "<task-notification",
    "<system-reminder",
    "<command-",
    "<local-command",
    "<scheduled",
    "This session is being continued",
)


class AgreementRefused(Exception):
    """A record cannot be made: it would cite something the hook never logged."""


@dataclass(frozen=True)
class PromptEntry:
    id: str
    time: datetime
    session_id: str
    prompt_id: str
    transcript_path: str
    cwd: str
    prompt: str
    prompt_sha: str
    shape: str = "plain"
    truncated: bool = False


@dataclass(frozen=True)
class PromptLog:
    entries: tuple[PromptEntry, ...]
    #: False when a line was edited or removed after it was written.
    intact: bool = True

    def find(self, entry_id: str) -> PromptEntry | None:
        for entry in self.entries:
            if entry.id == entry_id:
                return entry
        return None


@dataclass(frozen=True)
class Record:
    id: str
    repo: str
    pr: int
    head_sha: str
    work: str
    paths: tuple[str, ...]
    actions: tuple[str, ...]
    message_id: str
    message_sha: str
    session_id: str
    created_at: str
    #: ``{"kind": "cross-session-message" | "memory", "ref": <path>}`` or None. A pointer, not an approval.
    handoff: dict | None = field(default=None)


def prompt_log_path(state_dir: Path | str | None = None) -> Path:
    return paths.agreement_dir(state_dir) / "prompts.jsonl"


def records_path(state_dir: Path | str | None = None) -> Path:
    return paths.agreement_dir(state_dir) / "agreements.jsonl"


def shape_of(prompt: str) -> str:
    opening = prompt.lstrip()
    return "wrapped" if opening.startswith(_WRAPPED_OPENINGS) else "plain"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _line_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _append_line(path: Path, build) -> object:
    """Append one line under an exclusive lock. ``build(prev_hash, tail_text)`` returns ``(line, result)``."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        size = os.fstat(fd).st_size
        start = max(0, size - _DEDUPE_TAIL_BYTES)
        tail = os.pread(fd, size - start, start).decode("utf-8", errors="replace") if size else ""
        lines = [ln for ln in tail.split("\n") if ln.strip()]
        if start and lines:
            lines = lines[1:]  # the first line of a mid-file read is partial
        prev = _line_hash(lines[-1]) if lines else ""
        line, result = build(prev, lines)
        if line:
            os.write(fd, (line + "\n").encode("utf-8"))
        return result
    finally:
        os.close(fd)


def append_prompt(
    state_dir: Path | str | None,
    *,
    session_id: str,
    prompt_id: str,
    transcript_path: str,
    cwd: str,
    prompt: str,
    now: datetime | None = None,
) -> str | None:
    """Append one prompt as received. Returns its id, or None when this prompt id is already logged."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    truncated = len(prompt) > MAX_PROMPT_CHARS
    kept = prompt[:MAX_PROMPT_CHARS] if truncated else prompt
    entry_id = sha(f"{session_id}\n{prompt_id}\n{moment.isoformat()}\n{prompt}")[:16]

    def build(prev: str, tail: list[str]):
        if prompt_id:
            for raw in tail[-50:]:
                try:
                    seen = json.loads(raw)
                except ValueError:
                    continue
                if seen.get("prompt_id") == prompt_id and seen.get("session_id") == session_id:
                    return "", None
        row = {
            "v": 1,
            "id": entry_id,
            "time": moment.isoformat(),
            "session_id": session_id,
            "prompt_id": prompt_id,
            "transcript_path": transcript_path,
            "cwd": cwd,
            "prompt": kept,
            "prompt_sha": sha(prompt),
            "shape": shape_of(prompt),
            "truncated": truncated,
            "prev": prev,
        }
        return json.dumps(row, sort_keys=True, ensure_ascii=False), entry_id

    return _append_line(prompt_log_path(state_dir), build)


def read_prompts(state_dir: Path | str | None = None) -> PromptLog:
    path = prompt_log_path(state_dir)
    if not path.is_file():
        return PromptLog(())
    entries: list[PromptEntry] = []
    intact = True
    prev = ""
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
            moment = parse_time(row["time"])
            if moment is None:
                raise ValueError("time")
            entry = PromptEntry(
                id=str(row["id"]),
                time=moment,
                session_id=str(row.get("session_id", "")),
                prompt_id=str(row.get("prompt_id", "")),
                transcript_path=str(row.get("transcript_path", "")),
                cwd=str(row.get("cwd", "")),
                prompt=str(row.get("prompt", "")),
                prompt_sha=str(row.get("prompt_sha", "")),
                shape=str(row.get("shape", "plain")),
                truncated=bool(row.get("truncated", False)),
            )
        except (ValueError, KeyError, TypeError):
            intact = False
            continue
        # A line that was edited no longer matches its own hash, or the next line's chain.
        if row.get("prev", "") != prev or (
            not row.get("truncated") and sha(row.get("prompt", "")) != entry.prompt_sha
        ):
            intact = False
        prev = _line_hash(raw)
        entries.append(entry)
    return PromptLog(tuple(entries), intact)


# --- agreement records -------------------------------------------------------


def create_record(
    state_dir: Path | str | None,
    *,
    message_id: str,
    repo: str,
    pr: int,
    head_sha: str,
    work: str,
    paths: tuple[str, ...] | list[str],
    actions: tuple[str, ...] | list[str] = (),
    now: datetime | None = None,
    handoff: dict | None = None,
) -> Record:
    """Record that the owner agreed, citing one logged prompt by its id.

    The text of the agreement is never an argument: it is read from the log the hook
    wrote. A message the hook never logged cannot be cited.
    """
    entry = read_prompts(state_dir).find(message_id)
    if entry is None:
        raise AgreementRefused(f"message {message_id} is not in the prompt log")
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    record = Record(
        id="agr-" + uuid.uuid4().hex[:10],
        repo=repo,
        pr=int(pr),
        head_sha=head_sha,
        work=work.strip(),
        paths=tuple(str(p).strip() for p in paths),
        actions=tuple(
            sorted({str(a).strip().lower().replace("-", "_") for a in actions if str(a).strip()})
        ),
        message_id=entry.id,
        message_sha=entry.prompt_sha,
        session_id=entry.session_id,
        created_at=moment.isoformat(),
        handoff=handoff,
    )
    append_record(state_dir, record)
    return record


def append_record(state_dir: Path | str | None, record: Record) -> None:
    row = {"kind": "record", **asdict(record)}
    row["paths"] = list(record.paths)
    row["actions"] = list(record.actions)
    _append_line(
        records_path(state_dir), lambda prev, tail: (json.dumps(row, sort_keys=True), None)
    )


def attach_handoff(state_dir: Path | str | None, record_id: str, *, kind: str, ref: str) -> None:
    """Say where the session that got the work can be found. It points; it approves nothing."""
    row = {
        "kind": "handoff",
        "record_id": record_id,
        "handoff_kind": kind,
        "ref": ref,
        "time": datetime.now(UTC).isoformat(),
    }
    _append_line(
        records_path(state_dir), lambda prev, tail: (json.dumps(row, sort_keys=True), None)
    )


def read_records(state_dir: Path | str | None = None) -> list[Record]:
    path = records_path(state_dir)
    if not path.is_file():
        return []
    records: dict[str, Record] = {}
    handoffs: dict[str, dict] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(raw)
            if row.get("kind") == "handoff":
                handoffs[str(row["record_id"])] = {
                    "kind": str(row["handoff_kind"]),
                    "ref": str(row["ref"]),
                }
                continue
            records[str(row["id"])] = Record(
                id=str(row["id"]),
                repo=str(row["repo"]),
                pr=int(row["pr"]),
                head_sha=str(row.get("head_sha", "")),
                work=str(row.get("work", "")),
                paths=tuple(str(p) for p in row.get("paths", ())),
                actions=tuple(str(a) for a in row.get("actions", ())),
                message_id=str(row["message_id"]),
                message_sha=str(row.get("message_sha", "")),
                session_id=str(row.get("session_id", "")),
                created_at=str(row["created_at"]),
                handoff=row.get("handoff"),
            )
        except (ValueError, KeyError, TypeError):
            continue
    return [
        Record(**{**r.__dict__, "handoff": handoffs.get(r.id, r.handoff)}) for r in records.values()
    ]
