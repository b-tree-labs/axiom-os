# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Session readers for harnesses that keep conversations as JSON/JSONL files: Gemini CLI, Pi, Cline, Continue,
Cursor.

Verified by driving the real installs in a throwaway HOME (Gemini CLI 0.63.0, Pi 0.73.1, Cline CLI 3.0.70,
Continue ``cn`` 1.5.47) and reading back what they wrote; Cursor's agent transcripts were read from real files
on this machine (no install of the Cursor CLI was logged in). See :mod:`.sessions` for the verification table
and the rules every reader follows. Whole-document JSON formats (Cline, Continue) are bounded by
``MAX_DOC_BYTES``; JSONL formats are streamed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .sessions import (
    MAX_FILE_BYTES,  # noqa: F401 - re-exported for tests that adjust the caps
    SessionFormatError,
    SessionHeader,
    SessionTranscript,
    _Builder,
    _FileSessionReader,
    _iso_epoch,
    _iso_mtime,
    _jsonl,
    _text_of,
    _title_from,
    _tool_result_text,
)

MAX_DOC_BYTES = 256 * 1024 * 1024  # whole-document JSON is parsed in memory, so its cap is lower

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T")


class _DocReader(_FileSessionReader):
    def _check_size(self, path: Path) -> None:
        if path.stat().st_size > MAX_DOC_BYTES:
            raise SessionFormatError(
                f"document is larger than the {MAX_DOC_BYTES // (1024 * 1024)} MB import cap"
            )

    def _load_json(self, path: Path) -> Any:
        with open(path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8", errors="replace"))


# ---- Gemini CLI ---------------------------------------------------------------------------------


class GeminiSessionReader(_FileSessionReader):
    """``~/.gemini/tmp/<project>/chats/session-<ts>-<id8>.jsonl``. Line 1 is the session metadata; every later line
    is a message record (a record with an id seen again REPLACES the earlier one, in place), a
    ``{"$rewindTo": id}`` (drops that message and all after), or a ``{"$set": {...}}`` metadata update (``messages``
    inside it replaces the whole list). The same replay the CLI itself performs."""

    harness = "gemini"

    def _files(self) -> list[Path]:
        return sorted(
            self._walk(
                self.home / ".gemini" / "tmp",
                lambda rel, n: (
                    len(rel) == 2
                    and rel[1] == "chats"
                    and n.startswith("session-")
                    and n.endswith((".jsonl", ".json"))
                ),
            )
        )

    def _id_of(self, path: Path) -> str:
        return path.stem.rsplit("-", 1)[-1]

    def _cwd_for(self, path: Path) -> str:
        try:
            project = path.parent.parent.name
            data = json.loads((self.home / ".gemini" / "projects.json").read_text(encoding="utf-8"))
            for cwd, name in (data.get("projects") or {}).items():
                if name == project:
                    return str(cwd)
        except (OSError, ValueError, AttributeError):
            pass
        return ""

    def _convert(self, path: Path, light: bool = False) -> SessionTranscript | None:
        if path.suffix == ".json":
            raise SessionFormatError(
                "an older single-JSON Gemini session; only the JSONL format written by Gemini CLI 0.63 is verified"
            )
        b = _Builder(light)
        meta: dict[str, Any] = {}
        records: dict[str, dict[str, Any]] = {}

        def compact(rec: dict[str, Any]) -> dict[str, Any]:
            content = rec.get("content")
            parts = content if isinstance(content, list) else None
            # Gemini parts are {"text": ...} / {"functionResponse": ...} with no "type" key
            text = (
                content
                if isinstance(content, str)
                else "\n\n".join(
                    p["text"]
                    for p in parts or []
                    if isinstance(p, dict) and isinstance(p.get("text"), str)
                )
            )
            only_tool_results = bool(parts) and all(
                isinstance(p, dict) and "functionResponse" in p for p in parts
            )
            calls = []
            for call in rec.get("toolCalls") or []:
                if isinstance(call, dict):
                    calls.append(
                        (
                            b.call(str(call.get("name") or "?"), call.get("args")),
                            _tool_result_text(call.get("result"))
                            if call.get("result") is not None
                            else "",
                        )
                    )
            return {
                "type": rec.get("type"),
                "text": text,
                "ts": str(rec.get("timestamp") or ""),
                "tool_results_only": only_tool_results,
                "calls": calls,
                "thoughts": bool(rec.get("thoughts")),
            }

        for rec in _jsonl(path, b):
            rewind = rec.get("$rewindTo")
            if isinstance(rewind, str):
                if rewind in records:
                    keys = list(records)
                    for k in keys[keys.index(rewind) :]:
                        del records[k]
                else:
                    records.clear()
            elif isinstance(rec.get("id"), str):
                records[rec["id"]] = compact(rec)
            elif isinstance(rec.get("$set"), dict):
                update = rec["$set"]
                if isinstance(update.get("messages"), list):
                    records.clear()
                    for m in update["messages"]:
                        if isinstance(m, dict) and isinstance(m.get("id"), str):
                            records[m["id"]] = compact(m)
                meta.update({k: v for k, v in update.items() if k != "messages"})
            elif isinstance(rec.get("sessionId"), str) and isinstance(rec.get("projectHash"), str):
                meta.update(rec)
        if meta.get("kind") not in (None, "main"):
            return None  # a subagent's own log, not a conversation the person had
        if not meta.get("sessionId") and not records:
            raise SessionFormatError("no Gemini session metadata or messages in this file")
        for r in records.values():
            kind, ts = r["type"], r["ts"]
            if kind == "user":
                if r["tool_results_only"]:
                    continue  # the same result is carried on the gemini message's toolCalls
                if r["text"].lstrip().startswith("<session_context>"):
                    b.bump("harness_context_dropped")
                elif r["text"].strip():
                    b.user(r["text"], ts)
            elif kind == "gemini":
                if r["thoughts"]:
                    b.bump("thinking_dropped")
                b.assistant_piece(r["text"], ts)
                for call_text, result_text in r["calls"]:
                    b.bump("tool_calls")
                    b.assistant_piece(call_text, ts)
                    if result_text:
                        b.bump("tool_results")
                        b.assistant_piece(result_text, ts)
        messages = b.done()
        header = SessionHeader(
            harness=self.harness,
            source_session_id=self._id_of(path),
            title=_title_from(messages, str(meta.get("summary") or "")),
            created_at=str(meta.get("startTime") or "") or _iso_mtime(path),
            updated_at=str(meta.get("lastUpdated") or "") or _iso_mtime(path),
            message_count=len(messages),
            path=path,
            cwd=self._cwd_for(path),
        )
        return SessionTranscript(header, messages, b.lossy)


# ---- Pi -----------------------------------------------------------------------------------------

PI_MAX_VERSION = 3


class PiSessionReader(_FileSessionReader):
    """``~/.pi/agent/sessions/--<cwd>--/<timestamp>_<uuid>.jsonl``: a tree of entries (``id`` / ``parentId``). The
    conversation is the path from the root to the newest entry (the branch the person is on); abandoned branches
    are not imported. Session format versions 1-3 are read; a newer one is refused."""

    harness = "pi"

    def _files(self) -> list[Path]:
        return sorted(
            self._walk(
                self.home / ".pi" / "agent" / "sessions",
                lambda rel, n: len(rel) == 1 and n.endswith(".jsonl"),
            )
        )

    def _id_of(self, path: Path) -> str:
        stem = path.stem
        return stem.split("_", 1)[1] if "_" in stem else stem

    def _convert(self, path: Path, light: bool = False) -> SessionTranscript | None:
        b = _Builder(light)
        header: dict[str, Any] | None = None
        order: list[
            tuple[str | None, str | None]
        ] = []  # (id, parentId) of every entry, in file order
        name = ""
        # pass 1: header, the tree shape and the session name (small per entry)
        for rec in _jsonl(path, b):
            if header is None:
                if rec.get("type") != "session":
                    raise SessionFormatError("the first record is not a Pi session header")
                version = rec.get("version", 1)
                if not isinstance(version, int) or version > PI_MAX_VERSION:
                    raise SessionFormatError(
                        f"Pi session format version {version!r} is newer than the verified {PI_MAX_VERSION}"
                    )
                header = rec
                continue
            if rec.get("type") == "session_info" and isinstance(rec.get("name"), str):
                name = rec["name"]
            if rec.get("type") != "session":
                order.append(
                    (rec.get("id") if isinstance(rec.get("id"), str) else None, rec.get("parentId"))
                )
        if header is None:
            return None
        linear = any(
            i is None for i, _ in order
        )  # version 1: no ids, file order is the conversation
        keep: set[str] | None = None
        if not linear and order:
            parent = {i: p for i, p in order if i is not None}
            keep, cur, hops = set(), order[-1][0], 0
            while cur is not None and cur in parent and cur not in keep and hops <= len(parent):
                keep.add(cur)
                cur = parent[cur]
                hops += 1
        # pass 2: only the entries on the active path become messages
        b = _Builder(light)
        first = True
        for rec in _jsonl(path, b):
            if first:
                first = False
                continue
            if rec.get("type") != "message":
                continue
            if keep is not None and rec.get("id") not in keep:
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            ts = str(rec.get("timestamp") or "")
            role, content = msg.get("role"), msg.get("content")
            if role == "user":
                if isinstance(content, list) and any(
                    isinstance(x, dict) and x.get("type") == "image" for x in content
                ):
                    b.bump("attachments_dropped")
                text = _text_of(content, ("text",))
                if text.strip():
                    b.user(text, ts)
            elif role == "assistant":
                if isinstance(content, str):
                    b.assistant_piece(content, ts)
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        b.assistant_piece(str(block.get("text") or ""), ts)
                    elif block.get("type") == "toolCall":
                        b.bump("tool_calls")
                        b.assistant_piece(
                            b.call(str(block.get("name") or "?"), block.get("arguments")), ts
                        )
                    elif block.get("type") == "thinking":
                        b.bump("thinking_dropped")
            elif role == "toolResult":
                b.bump("tool_results")
                b.assistant_piece(_tool_result_text(_text_of(content, ("text",))), ts)
            elif role == "bashExecution":
                b.bump("tool_calls")
                b.bump("tool_results")
                b.assistant_piece(b.call("bash", {"command": str(msg.get("command") or "")}), ts)
                b.assistant_piece(_tool_result_text(str(msg.get("output") or "")), ts)
            else:  # custom / branchSummary / compactionSummary: the harness's own bookkeeping
                b.bump("harness_context_dropped")
        messages = b.done()
        out = SessionHeader(
            harness=self.harness,
            source_session_id=self._id_of(path),
            title=_title_from(messages, name),
            created_at=str(header.get("timestamp") or "") or _iso_mtime(path),
            updated_at=_iso_mtime(path),
            message_count=len(messages),
            path=path,
            cwd=str(header.get("cwd") or ""),
            format_version=str(header.get("version", 1)),
        )
        lossy = {k: v for k, v in b.lossy.items()}
        return SessionTranscript(out, messages, lossy)


# ---- Cline (CLI v3) -----------------------------------------------------------------------------

CLINE_MAX_VERSION = 1
_USER_INPUT = re.compile(r"<user_input[^>]*>(.*)</user_input>", re.DOTALL)


class ClineSessionReader(_DocReader):
    """Cline CLI 3.x: ``~/.cline/data/sessions/<id>/<id>.messages.json`` (Claude-style content blocks) next to
    ``<id>.json`` (title, cwd, times). The older VS Code extension keeps ``tasks/<id>/api_conversation_history.json``
    in a different shape; that is NOT read (no install on hand to verify)."""

    harness = "cline"

    def _files(self) -> list[Path]:
        return sorted(
            self._walk(
                self.home / ".cline" / "data" / "sessions",
                lambda rel, n: len(rel) == 1 and n.endswith(".messages.json"),
            )
        )

    def _id_of(self, path: Path) -> str:
        return path.name[: -len(".messages.json")]

    def _convert(self, path: Path, light: bool = False) -> SessionTranscript | None:
        doc = self._load_json(path)
        if not isinstance(doc, dict) or not isinstance(doc.get("messages"), list):
            raise SessionFormatError("no messages list in this Cline session file")
        version = doc.get("version", 1)
        if not isinstance(version, int) or version > CLINE_MAX_VERSION:
            raise SessionFormatError(
                f"Cline messages format version {version!r} is newer than the verified {CLINE_MAX_VERSION}"
            )
        meta: dict[str, Any] = {}
        try:
            loaded = self._load_json(path.with_name(self._id_of(path) + ".json"))
            meta = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            meta = {}
        if meta.get("is_subagent") or meta.get("parent_session_id"):
            return None
        meta_version = meta.get("version", 1)
        if isinstance(meta_version, int) and meta_version > CLINE_MAX_VERSION:
            raise SessionFormatError(
                f"Cline session metadata version {meta_version!r} is newer than the verified {CLINE_MAX_VERSION}"
            )
        b = _Builder(light)
        for m in doc["messages"]:
            if not isinstance(m, dict):
                continue
            ts = _iso_epoch(m.get("ts"), millis=True)
            content = m.get("content")
            blocks = (
                content
                if isinstance(content, list)
                else [{"type": "text", "text": content}]
                if isinstance(content, str)
                else []
            )
            if m.get("role") == "user":
                texts = []
                for blk in blocks:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") == "text":
                        texts.append(str(blk.get("text") or ""))
                    elif blk.get("type") == "tool_result":
                        b.bump("tool_results")
                        b.assistant_piece(_tool_result_text(blk.get("content")), ts)
                    elif blk.get("type") == "image":
                        b.bump("attachments_dropped")
                text = "\n\n".join(t for t in texts if t.strip())
                if not text:
                    continue
                if text.lstrip().startswith("[SYSTEM]"):
                    b.bump("harness_context_dropped")
                    continue
                found = _USER_INPUT.search(text)
                b.user((found.group(1) if found else text).strip(), ts)
            elif m.get("role") == "assistant":
                for blk in blocks:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") == "text":
                        b.assistant_piece(str(blk.get("text") or ""), ts)
                    elif blk.get("type") == "tool_use":
                        b.bump("tool_calls")
                        b.assistant_piece(b.call(str(blk.get("name") or "?"), blk.get("input")), ts)
                    elif blk.get("type") in ("thinking", "redacted_thinking", "reasoning"):
                        b.bump("thinking_dropped")
        messages = b.done()
        info = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
        header = SessionHeader(
            harness=self.harness,
            source_session_id=self._id_of(path),
            title=_title_from(messages, str(info.get("title") or "")),
            created_at=str(meta.get("started_at") or "") or _iso_mtime(path),
            updated_at=str(doc.get("updated_at") or "") or _iso_mtime(path),
            message_count=len(messages),
            path=path,
            cwd=str(meta.get("cwd") or meta.get("workspace_root") or ""),
            format_version=str(version),
        )
        return SessionTranscript(header, messages, b.lossy)


# ---- Continue -----------------------------------------------------------------------------------


class ContinueSessionReader(_DocReader):
    """Continue (``cn`` CLI and the IDE extension share it): ``~/.continue/sessions/<uuid>.json`` with a
    ``history`` list of ``{"message": {role, content, toolCalls?}}`` entries, plus a ``sessions.json`` index."""

    harness = "continue"

    def _files(self) -> list[Path]:
        return sorted(
            self._walk(
                self.home / ".continue" / "sessions",
                lambda rel, n: not rel and n.endswith(".json") and n != "sessions.json",
            )
        )

    def _index_created(self, sid: str) -> str:
        try:
            idx = json.loads(
                (self.home / ".continue" / "sessions" / "sessions.json").read_text(encoding="utf-8")
            )
            for e in idx if isinstance(idx, list) else []:
                if isinstance(e, dict) and e.get("sessionId") == sid:
                    return _iso_epoch(e.get("dateCreated"), millis=True)
        except (OSError, ValueError):
            pass
        return ""

    def _convert(self, path: Path, light: bool = False) -> SessionTranscript | None:
        doc = self._load_json(path)
        if not isinstance(doc, dict) or not isinstance(doc.get("history"), list):
            raise SessionFormatError("no history list in this Continue session file")
        b = _Builder(light)
        for item in doc["history"]:
            msg = item.get("message") if isinstance(item, dict) else None
            if not isinstance(msg, dict):
                continue
            role, content = msg.get("role"), msg.get("content")
            text = content if isinstance(content, str) else _text_of(content, ("text",))
            if isinstance(content, list) and any(
                isinstance(x, dict) and x.get("type") in ("imageUrl", "image") for x in content
            ):
                b.bump("attachments_dropped")
            if role == "user":
                if text.strip():
                    b.user(text, "")
            elif role == "assistant":
                b.assistant_piece(text, "")
                for call in msg.get("toolCalls") or []:
                    fn = (
                        call.get("function")
                        if isinstance(call, dict) and isinstance(call.get("function"), dict)
                        else {}
                    )
                    b.bump("tool_calls")
                    b.assistant_piece(b.call(str(fn.get("name") or "?"), fn.get("arguments")), "")
            elif role == "tool":
                b.bump("tool_results")
                b.assistant_piece(_tool_result_text(content if content is not None else ""), "")
            elif role == "thinking":
                b.bump("thinking_dropped")
        messages = b.done()
        title = str(doc.get("title") or "")
        header = SessionHeader(
            harness=self.harness,
            source_session_id=path.stem,
            title=_title_from(
                messages, "" if title.strip().lower() in ("", "untitled session") else title
            ),
            created_at=self._index_created(path.stem) or _iso_mtime(path),
            updated_at=_iso_mtime(path),
            message_count=len(messages),
            path=path,
            cwd=str(doc.get("workspaceDirectory") or ""),
        )
        return SessionTranscript(header, messages, b.lossy)


# ---- Cursor (IDE agent transcripts) ---------------------------------------------------------------

_USER_QUERY = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL)


class CursorSessionReader(_FileSessionReader):
    """``~/.cursor/projects/<slug>/agent-transcripts/<uuid>/<uuid>.jsonl``: one ``{"role", "message": {"content":
    [blocks]}}`` record per turn, plus ``{"type": "turn_ended"}`` markers. Tool calls are recorded; tool RESULTS
    are not (nothing to count or drop). Sub-agent logs (``<uuid>/subagents/``) are not conversations. The Cursor
    IDE's chat-mode history lives in ``state.vscdb`` (bubbles) and the CLI's in ``~/.cursor/chats``; neither is
    read (the second was not present to verify, the first is an app-internal key-value schema)."""

    harness = "cursor"

    def _files(self) -> list[Path]:
        return sorted(
            self._walk(
                self.home / ".cursor" / "projects",
                lambda rel, n: (
                    len(rel) == 3 and rel[1] == "agent-transcripts" and n == rel[2] + ".jsonl"
                ),
            )
        )

    def _convert(self, path: Path, light: bool = False) -> SessionTranscript | None:
        b = _Builder(light)
        stamp = _iso_mtime(path)
        for rec in _jsonl(path, b):
            msg = rec.get("message")
            if rec.get("role") not in ("user", "assistant") or not isinstance(msg, dict):
                continue
            content = msg.get("content")
            blocks = (
                content
                if isinstance(content, list)
                else [{"type": "text", "text": content}]
                if isinstance(content, str)
                else []
            )
            if rec["role"] == "user":
                texts = []
                for blk in blocks:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        raw = str(blk.get("text") or "")
                        found = _USER_QUERY.search(raw)
                        if found:
                            texts.append(found.group(1).strip())
                        elif raw.lstrip().startswith("<"):
                            b.bump("harness_context_dropped")
                        elif raw.strip():
                            texts.append(raw.strip())
                    elif isinstance(blk, dict) and blk.get("type") in ("image", "file"):
                        b.bump("attachments_dropped")
                if texts:
                    b.user("\n\n".join(texts), "")
            else:
                for blk in blocks:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") == "text":
                        b.assistant_piece(str(blk.get("text") or ""), "")
                    elif blk.get("type") == "tool_use":
                        b.bump("tool_calls")
                        b.assistant_piece(b.call(str(blk.get("name") or "?"), blk.get("input")), "")
                    elif blk.get("type") in ("thinking", "redacted_thinking"):
                        b.bump("thinking_dropped")
        messages = b.done()
        header = SessionHeader(
            harness=self.harness,
            source_session_id=path.stem,
            title=_title_from(messages),
            created_at=stamp,
            updated_at=stamp,
            message_count=len(messages),
            path=path,
            cwd="",
        )
        return SessionTranscript(header, messages, b.lossy)


__all__ = [
    "ClineSessionReader",
    "ContinueSessionReader",
    "CursorSessionReader",
    "GeminiSessionReader",
    "PiSessionReader",
]
