# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Instructor ↔ student question threads.

Tier C3 — gives both sides an asynchronous, tracked channel for
follow-up questions. A thread is a 1:1 conversation between the
instructor and one student; either party can open it, either can
reply, status moves ``open`` → ``answered`` → ``closed``. Later
tiers can add cohort-wide broadcasts and multi-party threads on top
of this primitive.

Storage layout under ``<base_dir>``::

    <base_dir>/threads/<thread_id>.json

One JSON per thread keeps listing cheap (dir scan) and makes
per-thread inspection easy for the instructor.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# IDs
# ---------------------------------------------------------------------------


def new_thread_id() -> str:
    """Short url-safe id. 12 bytes = 96 bits = collision-safe forever.

    Excludes leading '-' and '_' so the id is always argparse-safe as a
    positional arg on Python 3.11/3.12 (where argparse mis-parses
    dash-leading positionals as unknown options).
    """
    while True:
        tid = secrets.token_urlsafe(9)  # ~12 chars
        if tid[0] not in ("-", "_"):
            return tid


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThreadMessage:
    author_role: str  # "instructor" | "student"
    author_id: str    # student_id or instructor handle
    text: str
    timestamp: str    # ISO 8601 with timezone


@dataclass(frozen=True)
class Thread:
    thread_id: str
    classroom_id: str
    student_id: str           # the student half of the 1:1
    opened_by: str            # "instructor" | "student"
    status: str               # "open" | "answered" | "closed"
    opened_at: str
    messages: list[ThreadMessage] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@dataclass
class ThreadStore:
    base_dir: Path

    @property
    def _threads_dir(self) -> Path:
        return self.base_dir / "threads"

    def _path(self, thread_id: str) -> Path:
        return self._threads_dir / f"{thread_id}.json"

    # ---- Public API ----

    def save(self, thread: Thread) -> None:
        self._threads_dir.mkdir(parents=True, exist_ok=True)
        self._path(thread.thread_id).write_text(
            json.dumps(_thread_to_dict(thread), indent=2)
        )

    def get(self, thread_id: str) -> Thread | None:
        path = self._path(thread_id)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
        return _thread_from_dict(raw)

    def list_all(self) -> list[Thread]:
        if not self._threads_dir.is_dir():
            return []
        out: list[Thread] = []
        for path in self._threads_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            out.append(_thread_from_dict(raw))
        out.sort(key=lambda t: t.opened_at, reverse=True)
        return out

    def list_for_student(self, student_id: str) -> list[Thread]:
        return [t for t in self.list_all() if t.student_id == student_id]

    def list_open(self) -> list[Thread]:
        return [t for t in self.list_all() if t.status == "open"]

    def reply(self, thread_id: str, message: ThreadMessage) -> Thread:
        """Append ``message`` and transition status.

        Transition rules (v1, can grow later):
          - instructor reply to any open/answered thread → answered
          - student reply to any answered/open thread    → open

        Closed threads don't accept replies — caller should check or
        handle the raised ValueError.
        """
        thread = self.get(thread_id)
        if thread is None:
            raise KeyError(f"no thread with id {thread_id!r}")
        if thread.status == "closed":
            raise ValueError(f"thread {thread_id!r} is closed")

        new_status = (
            "answered" if message.author_role == "instructor" else "open"
        )
        updated = replace(
            thread,
            messages=list(thread.messages) + [message],
            status=new_status,
        )
        self.save(updated)
        return updated

    def close_thread(self, thread_id: str) -> Thread:
        thread = self.get(thread_id)
        if thread is None:
            raise KeyError(f"no thread with id {thread_id!r}")
        updated = replace(thread, status="closed")
        self.save(updated)
        return updated


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _thread_to_dict(t: Thread) -> dict:
    return {
        "thread_id": t.thread_id,
        "classroom_id": t.classroom_id,
        "student_id": t.student_id,
        "opened_by": t.opened_by,
        "status": t.status,
        "opened_at": t.opened_at,
        "messages": [asdict(m) for m in t.messages],
    }


def _thread_from_dict(raw: dict) -> Thread:
    messages = [
        ThreadMessage(
            author_role=str(m["author_role"]),
            author_id=str(m["author_id"]),
            text=str(m["text"]),
            timestamp=str(m["timestamp"]),
        )
        for m in raw.get("messages", [])
    ]
    return Thread(
        thread_id=str(raw["thread_id"]),
        classroom_id=str(raw["classroom_id"]),
        student_id=str(raw["student_id"]),
        opened_by=str(raw["opened_by"]),
        status=str(raw["status"]),
        opened_at=str(raw["opened_at"]),
        messages=messages,
    )


__all__ = [
    "Thread",
    "ThreadMessage",
    "ThreadStore",
    "new_thread_id",
    "now_iso",
]
