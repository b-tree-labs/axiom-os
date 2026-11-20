# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Session readers for harnesses that keep conversations in a SQLite database: OpenCode, Hermes, Goose.

Each store was verified by driving the real install (OpenCode 1.17.8, Hermes 0.20.4, Goose 1.53.0) in a throwaway
HOME against a local Ollama model and reading back the database it wrote; OpenCode's schema was also checked
against the real database on this machine. See :mod:`.sessions` for the verification table and the rules every
reader follows.

Opened ``mode=ro`` with ``PRAGMA query_only`` inside one read transaction, so a harness appending while we read
cannot give us a torn view, and nothing is written. The structure is checked first (required tables and columns);
a store that does not have them is refused with the missing names, never guessed at. Optional columns are used
when present, so a harness adding columns does not break the reader.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .base import SkippedSource
from .sessions import (
    SessionFormatError,
    SessionHeader,
    SessionTranscript,
    _Builder,
    _iso_epoch,
    _ReaderBase,
    _title_from,
    _tool_result_text,
)


def _open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
    conn.text_factory = lambda b: b.decode(
        "utf-8", "replace"
    )  # non-UTF-8 bytes degrade, they do not raise
    conn.isolation_level = None  # explicit BEGIN below: one snapshot for the whole read
    conn.execute("PRAGMA query_only=1")
    conn.execute("BEGIN")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608 - table names are literals here


def _check_structure(
    conn: sqlite3.Connection, required: dict[str, tuple[str, ...]]
) -> dict[str, set[str]]:
    seen: dict[str, set[str]] = {}
    missing: list[str] = []
    for table, cols in required.items():
        have = _columns(conn, table)
        seen[table] = have
        if not have:
            missing.append(f"table {table}")
            continue
        missing += [f"{table}.{c}" for c in cols if c not in have]
    if missing:
        raise SessionFormatError(
            "this store does not have what the reader expects: missing " + ", ".join(missing)
        )
    return seen


def _sel(have: set[str], names: tuple[str, ...]) -> str:
    """A select list that substitutes NULL for columns this version of the store does not have."""
    return ", ".join(n if n in have else f"NULL AS {n}" for n in names)


def _loads(text: Any) -> Any:
    try:
        return json.loads(text) if isinstance(text, (str, bytes)) else None
    except ValueError:
        return None


def _iso_text(value: Any) -> str:
    """An ISO string from a SQL timestamp text ('YYYY-MM-DD HH:MM:SS' is UTC); '' if unusable."""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return ""
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat()


class _SqliteSessionReader(_ReaderBase):
    required: dict[str, tuple[str, ...]] = {}

    def db_path(self) -> Path:  # pragma: no cover - overridden
        raise NotImplementedError

    def _session_rows(
        self, conn: sqlite3.Connection, have: dict[str, set[str]], limit: int
    ) -> list[dict[str, Any]]:
        raise NotImplementedError  # pragma: no cover

    def _session_row(
        self, conn: sqlite3.Connection, have: dict[str, set[str]], sid: str
    ) -> dict[str, Any] | None:
        raise NotImplementedError  # pragma: no cover

    def _convert(
        self,
        conn: sqlite3.Connection,
        have: dict[str, set[str]],
        row: dict[str, Any],
        light: bool = False,
    ) -> SessionTranscript:
        raise NotImplementedError  # pragma: no cover

    def _connect(self) -> tuple[sqlite3.Connection, dict[str, set[str]]] | None:
        path = self.db_path()
        if not path.exists():
            self.notes.append(
                f"{self.harness}: {path} does not exist (not installed, or it keeps its store elsewhere)"
            )
            return None
        conn = _open_ro(path)
        try:
            return conn, _check_structure(conn, self.required)
        except Exception:
            conn.close()
            raise

    def list_sessions(self, limit: int = 50) -> list[SessionHeader]:
        self.skipped, self.notes = [], []
        path = self.db_path()
        try:
            opened = self._connect()
        except SessionFormatError as exc:
            self.skipped.append(
                SkippedSource(source=str(path), reason=f"unsupported format: {exc}")
            )
            return []
        except (sqlite3.Error, OSError) as exc:
            self.skipped.append(SkippedSource(source=str(path), reason=f"unreadable: {exc}"))
            return []
        if opened is None:
            return []
        conn, have = opened
        headers: list[SessionHeader] = []
        try:
            for row in self._session_rows(conn, have, max(limit, 0)):
                key = f"{path}#{row['id']}"
                stamp = [row.get("stamp")]
                hit = self._cached(key, stamp)
                if hit is not None:
                    headers.append(hit)
                    continue
                try:
                    transcript = self._convert(conn, have, row, light=True)
                except (sqlite3.Error, ValueError, TypeError) as exc:
                    self.skipped.append(
                        SkippedSource(source=f"{path}#{row['id']}", reason=f"unreadable: {exc}")
                    )
                    continue
                if not transcript.messages:
                    self.skipped.append(
                        SkippedSource(
                            source=f"{path}#{row['id']}", reason="no conversation messages"
                        )
                    )
                    continue
                self._remember(key, stamp, transcript.header)
                headers.append(transcript.header)
        except sqlite3.Error as exc:
            self.skipped.append(SkippedSource(source=str(path), reason=f"unreadable: {exc}"))
        finally:
            conn.close()
        self._save_cache()
        return self._order(headers)

    def load_session(self, source_session_id: str) -> SessionTranscript | None:
        self.last_error = ""
        self.notes = []
        try:
            opened = self._connect()
        except SessionFormatError as exc:
            self.last_error = f"unsupported format: {exc}"
            return None
        except (sqlite3.Error, OSError) as exc:
            self.last_error = f"unreadable: {exc}"
            return None
        if opened is None:
            return None
        conn, have = opened
        try:
            row = self._session_row(conn, have, source_session_id)
            return self._convert(conn, have, row) if row is not None else None
        except (sqlite3.Error, ValueError, TypeError) as exc:
            self.last_error = f"unreadable: {exc}"
            return None
        finally:
            conn.close()


# ---- OpenCode -----------------------------------------------------------------------------------

_OPENCODE_UNTITLED = re.compile(r"^(New session|Child session) - \d{4}-\d{2}-\d{2}T")
#: Part types that carry no conversation (bookkeeping); anything else unknown is counted, not guessed at.
_OPENCODE_QUIET_PARTS = {
    "step-start",
    "step-finish",
    "snapshot",
    "patch",
    "agent",
    "retry",
    "compaction",
}


class OpenCodeSessionReader(_SqliteSessionReader):
    harness = "opencode"
    required = {
        "session": ("id", "title", "directory", "time_created", "time_updated"),
        "message": ("id", "session_id", "time_created", "data"),
        "part": ("id", "message_id", "time_created", "data"),
    }

    def db_path(self) -> Path:
        return self.home / ".local" / "share" / "opencode" / "opencode.db"

    def _session_rows(self, conn, have, limit):
        sel = _sel(
            have["session"],
            ("id", "title", "directory", "time_created", "time_updated", "version", "parent_id"),
        )
        where = "WHERE parent_id IS NULL" if "parent_id" in have["session"] else ""
        cur = conn.execute(
            f"SELECT {sel} FROM session {where} ORDER BY time_updated DESC, id LIMIT ?", (limit,)
        )  # noqa: S608
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur:
            d = dict(zip(cols, r, strict=True))
            d["stamp"] = d["time_updated"]
            out.append(d)
        return out

    def _session_row(self, conn, have, sid):
        sel = _sel(
            have["session"], ("id", "title", "directory", "time_created", "time_updated", "version")
        )
        cur = conn.execute(f"SELECT {sel} FROM session WHERE id = ?", (sid,))  # noqa: S608
        r = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], r, strict=True)) if r else None

    def _convert(self, conn, have, row, light=False):
        b = _Builder(light)
        cur = conn.execute(
            "SELECT m.id, m.time_created, m.data, p.data FROM message m LEFT JOIN part p ON p.message_id = m.id "
            "WHERE m.session_id = ? ORDER BY m.time_created, m.id, p.time_created, p.id",
            (row["id"],),
        )
        current: str | None = None
        role, ts = "", ""
        user_text: list[str] = []

        def close_message() -> None:
            if role == "user" and user_text:
                b.user("\n\n".join(user_text), ts)

        for mid, created, mdata, pdata in cur:
            if mid != current:
                close_message()
                current, user_text = mid, []
                meta = _loads(mdata) or {}
                role = meta.get("role") if isinstance(meta, dict) else ""
                ts = _iso_epoch(created, millis=True)
            part = _loads(pdata) if pdata is not None else None
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "text":
                text = str(part.get("text") or "")
                if part.get("synthetic") or part.get("ignored"):
                    b.bump("harness_context_dropped")
                elif role == "user":
                    user_text.append(text)
                elif role == "assistant":
                    b.assistant_piece(text, ts)
            elif kind == "tool" and role == "assistant":
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                b.bump("tool_calls")
                b.assistant_piece(b.call(str(part.get("tool") or "?"), state.get("input")), ts)
                if state.get("status") in ("completed", "error"):
                    b.bump("tool_results")
                    b.assistant_piece(
                        _tool_result_text(
                            state.get("output") if "output" in state else state.get("error")
                        ),
                        ts,
                    )
            elif kind == "reasoning":
                b.bump("thinking_dropped")
            elif kind == "file":
                b.bump("attachments_dropped")
            elif kind not in _OPENCODE_QUIET_PARTS:
                b.bump("unknown_blocks")
        close_message()
        messages = b.done()
        title = (
            ""
            if _OPENCODE_UNTITLED.match(str(row.get("title") or ""))
            else str(row.get("title") or "")
        )
        header = SessionHeader(
            harness=self.harness,
            source_session_id=str(row["id"]),
            title=_title_from(messages, title),
            created_at=_iso_epoch(row.get("time_created"), millis=True),
            updated_at=_iso_epoch(row.get("time_updated"), millis=True),
            message_count=len(messages),
            path=self.db_path(),
            cwd=str(row.get("directory") or ""),
            format_version=str(row.get("version") or ""),
        )
        return SessionTranscript(header, messages, b.lossy)


# ---- Hermes -------------------------------------------------------------------------------------


class HermesSessionReader(_SqliteSessionReader):
    harness = "hermes"
    required = {
        "sessions": ("id", "started_at"),
        "messages": ("session_id", "role", "content"),
    }

    def db_path(self) -> Path:
        return self.home / ".hermes" / "state.db"

    def _schema_version(self, conn) -> str:
        try:
            r = conn.execute("SELECT * FROM schema_version LIMIT 1").fetchone()
            return str(r[0]) if r else ""
        except sqlite3.Error:
            return ""

    def _session_rows(self, conn, have, limit):
        s = have["sessions"]
        hidden = "WHERE COALESCE(hidden, 0) = 0" if "hidden" in s else ""
        sel = _sel(
            s, ("id", "title", "cwd", "started_at", "ended_at", "last_activity_at", "message_count")
        )
        cur = conn.execute(
            f"SELECT {sel} FROM sessions {hidden} "  # noqa: S608
            "ORDER BY COALESCE(last_activity_at, ended_at, started_at) DESC, id LIMIT ?",
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur:
            d = dict(zip(cols, r, strict=True))
            d["stamp"] = [
                d.get("last_activity_at"),
                d.get("ended_at"),
                d.get("started_at"),
                d.get("message_count"),
            ]
            out.append(d)
        return out

    def _session_row(self, conn, have, sid):
        sel = _sel(
            have["sessions"],
            ("id", "title", "cwd", "started_at", "ended_at", "last_activity_at", "message_count"),
        )
        cur = conn.execute(f"SELECT {sel} FROM sessions WHERE id = ?", (sid,))  # noqa: S608
        r = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], r, strict=True)) if r else None

    def _convert(self, conn, have, row, light=False):
        m = have["messages"]
        b = _Builder(light)
        sel = _sel(
            m,
            (
                "role",
                "content",
                "tool_calls",
                "tool_name",
                "timestamp",
                "active",
                "reasoning",
                "reasoning_content",
            ),
        )
        cur = conn.execute(
            f"SELECT {sel} FROM messages WHERE session_id = ? ORDER BY rowid", (row["id"],)
        )  # noqa: S608
        for (
            role,
            content,
            tool_calls,
            _tool_name,
            timestamp,
            active,
            reasoning,
            reasoning_content,
        ) in cur:
            if active == 0:
                continue  # rewound/compacted away by the harness itself
            ts = _iso_epoch(timestamp)
            if role == "user":
                if isinstance(content, str) and content.strip():
                    b.user(content, ts)
            elif role == "assistant":
                if isinstance(content, str):
                    b.assistant_piece(content, ts)
                calls = _loads(tool_calls)
                for call in calls if isinstance(calls, list) else []:
                    fn = (
                        call.get("function")
                        if isinstance(call, dict) and isinstance(call.get("function"), dict)
                        else {}
                    )
                    b.bump("tool_calls")
                    b.assistant_piece(b.call(str(fn.get("name") or "?"), fn.get("arguments")), ts)
                if (reasoning or "").strip() or (reasoning_content or "").strip():
                    b.bump("thinking_dropped")
            elif role == "tool":
                b.bump("tool_results")
                b.assistant_piece(_tool_result_text(content if content is not None else ""), ts)
            # role == "system": the harness's own instructions, never the conversation
        messages = b.done()
        header = SessionHeader(
            harness=self.harness,
            source_session_id=str(row["id"]),
            title=_title_from(messages, str(row.get("title") or "")),
            created_at=_iso_epoch(row.get("started_at")),
            updated_at=_iso_epoch(
                row.get("last_activity_at") or row.get("ended_at") or row.get("started_at")
            ),
            message_count=len(messages),
            path=self.db_path(),
            cwd=str(row.get("cwd") or ""),
            format_version=self._schema_version(conn),
        )
        return SessionTranscript(header, messages, b.lossy)


# ---- Goose --------------------------------------------------------------------------------------

_GOOSE_QUIET = {"systemNotification", "frontendToolRequest", "actionRequired"}
_GOOSE_THINKING = {"thinking", "redactedThinking", "reasoning"}


class GooseSessionReader(_SqliteSessionReader):
    harness = "goose"
    required = {
        "sessions": ("id", "working_dir"),
        "messages": ("session_id", "role", "content_json"),
    }

    def db_path(self) -> Path:
        return self.home / ".local" / "share" / "goose" / "sessions" / "sessions.db"

    def _session_rows(self, conn, have, limit):
        s = have["sessions"]
        where = "WHERE session_type = 'user'" if "session_type" in s else ""
        sel = _sel(s, ("id", "name", "working_dir", "created_at", "updated_at"))
        cur = conn.execute(
            f"SELECT {sel} FROM sessions {where} ORDER BY updated_at DESC, id LIMIT ?", (limit,)
        )  # noqa: S608
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur:
            d = dict(zip(cols, r, strict=True))
            d["stamp"] = [d.get("updated_at")]
            out.append(d)
        return out

    def _session_row(self, conn, have, sid):
        sel = _sel(have["sessions"], ("id", "name", "working_dir", "created_at", "updated_at"))
        cur = conn.execute(f"SELECT {sel} FROM sessions WHERE id = ?", (sid,))  # noqa: S608
        r = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], r, strict=True)) if r else None

    def _schema_version(self, conn) -> str:
        try:
            r = conn.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()
            return str(r[0]) if r else ""
        except sqlite3.Error:
            return ""

    def _convert(self, conn, have, row, light=False):
        m = have["messages"]
        b = _Builder(light)
        sel = _sel(m, ("role", "content_json", "created_timestamp", "metadata_json"))
        cur = conn.execute(
            f"SELECT {sel} FROM messages WHERE session_id = ? ORDER BY created_timestamp, rowid",  # noqa: S608
            (row["id"],),
        )
        for role, content_json, created, metadata_json in cur:
            meta = _loads(metadata_json)
            if isinstance(meta, dict) and meta.get("userVisible") is False:
                b.bump("harness_context_dropped")
                continue
            blocks = _loads(content_json)
            if not isinstance(blocks, list):
                continue
            ts = _iso_epoch(created)
            texts: list[str] = []
            for blk in blocks:
                if not isinstance(blk, dict):
                    continue
                kind = blk.get("type")
                if kind == "text":
                    texts.append(str(blk.get("text") or ""))
                elif kind == "toolRequest":
                    value = (
                        ((blk.get("toolCall") or {}).get("value") or {})
                        if isinstance(blk.get("toolCall"), dict)
                        else {}
                    )
                    b.bump("tool_calls")
                    b.assistant_piece(
                        b.call(str(value.get("name") or "?"), value.get("arguments")), ts
                    )
                elif kind == "toolResponse":
                    result = (
                        (blk.get("toolResult") or {})
                        if isinstance(blk.get("toolResult"), dict)
                        else {}
                    )
                    b.bump("tool_results")
                    b.assistant_piece(_tool_result_text(result.get("value")), ts)
                elif kind in _GOOSE_THINKING:
                    b.bump("thinking_dropped")
                elif kind == "image":
                    b.bump("attachments_dropped")
                elif kind not in _GOOSE_QUIET:
                    b.bump("unknown_blocks")
            text = "\n\n".join(t for t in texts if t.strip())
            if role == "user" and text:
                b.user(text, ts)
            elif role == "assistant" and text:
                b.assistant_piece(text, ts)
        messages = b.done()
        header = SessionHeader(
            harness=self.harness,
            source_session_id=str(row["id"]),
            title=_title_from(messages, str(row.get("name") or "")),
            created_at=_iso_text(row.get("created_at")),
            updated_at=_iso_text(row.get("updated_at")),
            message_count=len(messages),
            path=self.db_path(),
            cwd=str(row.get("working_dir") or ""),
            format_version=self._schema_version(conn),
        )
        return SessionTranscript(header, messages, b.lossy)


__all__ = ["GooseSessionReader", "HermesSessionReader", "OpenCodeSessionReader"]
