# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Session readers: whole conversations out of another harness's on-disk store (ADR-087 D8, additive).

The absorb adapters in this package yield memory FRAGMENTS (notes, goals, instruction files). A session is a
different thing: an ordered conversation. ``/renew`` needs to list those and carry one on, so this module adds
the second capability next to the first, with the same law: **read-only**. A reader opens source files for
reading (SQLite stores with ``mode=ro`` + ``query_only``), never writes, never touches mtime, and returns plain
values; the caller decides what to do with them (``/renew`` converts one into a NEW native chat session; the
source is never altered). SQLite itself may touch a ``-shm`` lock file while a harness holds a WAL open; the
database and its ``-wal`` are never modified.

A reader satisfies :class:`axiom.memory.absorb.base.SessionCapable`:

``list_sessions(limit)``
    headers, newest first (only the newest ``limit`` are parsed, so a large history stays cheap; an optional
    on-disk header cache, :mod:`.session_index`, makes repeat listings near-instant);
``load_session(source_session_id)``
    the full converted transcript, or ``None`` if there is no such session (``reader.last_error`` says why when
    the reason is a refused format rather than an absent session).

Where each harness keeps conversations, and how we know (``verified-by``: ``run`` = a real install was driven in
a throwaway HOME and the session it wrote was read back; ``real files`` = real session files on this machine;
``docs`` = the vendor's published format only; ``unverified`` = nothing checked). A reader ships ONLY for rows
verified by ``run`` or ``real files``. Tool versions are those the formats were verified against, 2026-10-08.

====================  ============================================================  ==============  ===============  ========
harness               path pattern (under the user's home)                          format          verified-by      reader
====================  ============================================================  ==============  ===============  ========
claude-code           .claude/projects/<slug>/<uuid>.jsonl                          JSONL           real files       shipped
codex                 .codex/sessions/Y/M/D/rollout-*.jsonl, archived_sessions/     JSONL           real files       shipped
opencode 1.17.8       .local/share/opencode/opencode.db (session/message/part)      SQLite          run + files      shipped
hermes 0.20.4         .hermes/state.db (sessions/messages, schema_version 26)       SQLite          run              shipped
goose 1.53.0          .local/share/goose/sessions/sessions.db (sessions/messages)   SQLite          run              shipped
gemini-cli 0.63.0     .gemini/tmp/<project>/chats/session-*.jsonl                   JSONL           run (*)          shipped
pi 0.73.1             .pi/agent/sessions/--<cwd>--/<ts>_<uuid>.jsonl (v3 tree)      JSONL           run              shipped
cline 3.0.70 (CLI)    .cline/data/sessions/<id>/<id>.messages.json (+ <id>.json)    JSON            run              shipped
continue (cn 1.5.47)  .continue/sessions/<uuid>.json (+ sessions.json index)        JSON            run              shipped
cursor (IDE agent)    .cursor/projects/<slug>/agent-transcripts/<uuid>/<uuid>.jsonl  JSONL          real files       shipped
cursor (CLI/chat UI)  .cursor/chats/*/store.db; globalStorage/state.vscdb bubbles   SQLite          unverified       none
aider 0.86.2          <project>/.aider.chat.history.md                              markdown        run             none (**)
letta code 0.34.6     server-side (Letta cloud or self-hosted server)               remote API      unverified       none
cline/roo (VS Code)   globalStorage/<ext>/tasks/<id>/api_conversation_history.json  JSON            docs            none
grok (Grok Bot)       app userData (app never opened on this machine)               unknown         unverified       none (***)
====================  ============================================================  ==============  ===============  ========

(*) Gemini CLI has no local-model switch, so its session was written by the real CLI against a local stub of the
Gemini HTTP API in a throwaway HOME (no login, no credentials); the file format is the CLI's own.
(***) The only Grok client installed is the Grok Bot desktop app (bundle com.anysphere.sand); it has never been
launched on this machine, so there are no conversations or format to verify. No Grok CLI is installed.
(**) aider writes a markdown log inside each project directory; there is no central place to enumerate them, so
there is nothing to list. See :data:`HARNESS_STATUS` for the exact per-harness record the ``/renew`` messages use.

What does not map faithfully is KEPT AS TEXT, and counted in ``SessionTranscript.lossy``: a tool call becomes
``[tool call: name(hint)]`` inside the assistant message, a tool result becomes ``[tool result: N chars, not
imported]`` (the content is not copied: it is large and the likeliest place for a secret), reasoning/thinking
blocks and attachments are dropped, secrets in tool-call hints are withheld.

Robustness rules every reader follows (the conformance suite in ``chat/tests/test_renew_conformance.py`` pins
them per harness):

* a corrupt, truncated, empty or non-UTF-8 session file is skipped with a named reason (``reader.skipped``) and
  the rest keep listing; invalid JSONL lines are skipped and counted (``lossy["invalid_lines"]``); a partial
  last line (a harness mid-append) is a consistent prefix, counted as ``partial_trailing_line``;
* a format VERSION newer than the one verified (files with an explicit version field: Pi, Cline) or a store
  whose required tables/columns are absent (SQLite harnesses) is refused loudly: no partial import, the reason
  is on ``reader.skipped`` / ``reader.last_error``; ``SessionHeader.format_version`` records what was read and
  ``/renew`` stores it in the session's ``renewed_from`` provenance;
* bounded memory with STATED caps, never silent: one line over ``MAX_LINE_BYTES`` is skipped, a message over
  ``MAX_MESSAGE_CHARS`` is truncated, a transcript keeps its newest ``MAX_MESSAGES`` messages / ``MAX_TOTAL_CHARS``
  characters, a file over ``MAX_FILE_BYTES`` is refused. Each is counted in ``lossy`` and shown by ``/renew``;
* a missing, renamed or permission-denied harness directory lists nothing and says why (``reader.notes``);
  symlinks are not followed when walking;
* ordering: newest first by ``updated_at`` (the harness's own last-activity stamp where it keeps one, else the
  file mtime). An unparsable or missing stamp sorts oldest; ties break by source id, so order is deterministic
  under clock skew. Message order is the harness's own record order, never re-sorted by timestamp.

Harnesses whose memory adapters exist but whose conversation format is not read here have NO session reader;
``session_readers`` simply omits them and ``HARNESS_STATUS`` says what is missing.
"""

from __future__ import annotations

import json
import os
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .base import SkippedSource

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HINT_LIMIT = 80
_TITLE_LIMIT = 60

#: Stated caps (module-level so a test or an operator can see and change them). Hitting one is never silent: it is
#: counted in ``SessionTranscript.lossy`` and ``/renew`` says so.
MAX_LINE_BYTES = 8 * 1024 * 1024  # one JSONL line larger than this is skipped
MAX_FILE_BYTES = 1024 * 1024 * 1024  # a session file larger than this is refused
MAX_MESSAGES = 10_000  # newest kept
MAX_MESSAGE_CHARS = 100_000  # one message longer than this is cut
MAX_TOTAL_CHARS = 20_000_000  # newest kept

#: Codex injects these as "user" messages; they are the harness talking, not the person.
_CODEX_INJECTED = (
    "<environment_context>",
    "<user_instructions>",
    "# AGENTS.md instructions",
    "<permissions",
)


class SessionFormatError(Exception):
    """A session (or a whole store) is in a format this reader will not guess at. The message is the reason."""


@dataclass(frozen=True)
class SessionHeader:
    harness: str
    source_session_id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int
    path: Path
    cwd: str = ""
    #: What the store says about its own format/app version ("" when the format carries none). Informational
    #: for SQLite/JSONL stores that are checked structurally; gating for the ones with an explicit version.
    format_version: str = ""


@dataclass(frozen=True)
class SessionMessage:
    role: str  # user | assistant
    content: str
    timestamp: str = ""


@dataclass
class SessionTranscript:
    header: SessionHeader
    messages: list[SessionMessage] = field(default_factory=list)
    #: What did not map faithfully: {"tool_calls": n, "tool_results": n, "thinking_dropped": n, ...}
    lossy: dict[str, int] = field(default_factory=dict)


# ---- helpers ------------------------------------------------------------------------------------


def _iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()


def _iso_epoch(value: Any, *, millis: bool = False) -> str:
    """An ISO-8601 UTC string from epoch seconds/millis, or ``""`` when it is not a usable number."""
    try:
        seconds = float(value) / (1000.0 if millis else 1.0)
        return datetime.fromtimestamp(seconds, UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _looks_secret(text: str) -> bool:
    try:
        from axiom.memory.serving import looks_like_secret

        return bool(looks_like_secret(text))
    except Exception:  # noqa: BLE001 - if the detector cannot run, assume the worst
        return True


def _args_hint(args: Any) -> str:
    """A short, secret-safe hint of what a tool was called with: the first string value, if it is safe."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            args = {}
    if not isinstance(args, dict):
        return ""
    for key, value in args.items():
        if isinstance(value, list) and value and isinstance(value[0], str):
            value = value[0]  # e.g. a list of commands: the first one is the hint
        if isinstance(value, str) and value.strip():
            if _looks_secret(value) or _looks_secret(f"{key}={value}"):
                return f"{key}=(withheld)"
            return f"{key}={_one_line(value, _HINT_LIMIT)}"
    return ""


def _tool_call_text(name: str, args: Any) -> str:
    return f"[tool call: {name}({_args_hint(args)})]"


def _tool_result_text(body: Any) -> str:
    if isinstance(body, str):
        size = len(body)
    else:
        try:
            size = len(json.dumps(body, default=str))
        except (TypeError, ValueError):
            size = 0
    return f"[tool result: {size} chars, not imported]"


def _text_of(content: Any, kinds: tuple[str, ...]) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict)
            and b.get("type") in kinds
            and isinstance(b.get("text"), str)
            and b.get("text")
        )
    return ""


def _jsonl(path: Path, builder: _Builder | None = None):
    """Valid JSON-object lines of ``path``. Memory is bounded by ``MAX_LINE_BYTES``: an oversize line is skipped
    (never buffered whole); a line that is not a JSON object is skipped; both are counted on ``builder``. A last
    line with no newline that does not parse is a harness mid-append: a consistent prefix, counted as such."""
    bump = builder.bump if builder is not None else (lambda *_a, **_k: None)
    with open(path, "rb") as fh:
        while True:
            raw = fh.readline(MAX_LINE_BYTES + 1)
            if not raw:
                return
            complete = raw.endswith(b"\n")
            if len(raw) > MAX_LINE_BYTES and not complete:
                bump("oversize_lines_skipped")
                while raw and not raw.endswith(
                    b"\n"
                ):  # drain the rest of the line without keeping it
                    raw = fh.readline(MAX_LINE_BYTES)
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                bump("partial_trailing_line" if not complete else "invalid_lines")
                continue
            if isinstance(rec, dict):
                yield rec
            else:
                bump("invalid_lines")


class _Builder:
    """Accumulates messages; consecutive assistant pieces (text, tool calls, results) are one message. Memory is
    bounded: see the ``MAX_*`` caps; every cut is counted in ``lossy``."""

    def __init__(self, light: bool = False) -> None:
        #: light=True is for listing: same message structure and count, but tool-call hints (and so the per-call
        #: secret scan) are skipped, which is most of the cost of converting a large history.
        self.light = light
        self.messages: deque[SessionMessage] = deque()
        self._chars = 0
        self._assistant: list[str] = []
        self._assistant_chars = 0
        self._assistant_ts = ""
        self.lossy: dict[str, int] = {}

    def bump(self, key: str, n: int = 1) -> None:
        self.lossy[key] = self.lossy.get(key, 0) + n

    def call(self, name: str, args: Any) -> str:
        return f"[tool call: {name}()]" if self.light else _tool_call_text(name, args)

    def _append(self, role: str, text: str, ts: str) -> None:
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS] + "\n[...cut: message longer than the import cap]"
            self.bump("long_messages_truncated")
        self.messages.append(SessionMessage(role, text, ts))
        self._chars += len(text)
        while len(self.messages) > 1 and (
            len(self.messages) > MAX_MESSAGES or self._chars > MAX_TOTAL_CHARS
        ):
            old = self.messages.popleft()
            self._chars -= len(old.content)
            self.bump("older_messages_omitted")

    def user(self, text: str, ts: str) -> None:
        self.flush()
        self._append("user", text, ts)

    def assistant_piece(self, text: str, ts: str) -> None:
        if not text.strip():
            return
        if self._assistant_chars > MAX_MESSAGE_CHARS:
            return  # the message is already over the cap; flush() will say so
        if not self._assistant:
            self._assistant_ts = ts
        self._assistant.append(text)
        self._assistant_chars += len(text)

    def flush(self) -> None:
        if self._assistant:
            self._append("assistant", "\n\n".join(self._assistant), self._assistant_ts)
            self._assistant = []
            self._assistant_chars = 0

    def done(self) -> list[SessionMessage]:
        self.flush()
        return list(self.messages)


def _title_from(messages: list[SessionMessage], explicit: str = "") -> str:
    if explicit.strip():
        return _one_line(explicit, _TITLE_LIMIT)
    for m in messages:
        if m.role == "user" and m.content.strip():
            return _one_line(m.content.strip().splitlines()[0], _TITLE_LIMIT)
    return ""


class _ReaderBase:
    """What every reader shares: the home it reads under, the explanations it accumulates, the optional header
    cache, and the one deterministic newest-first ordering."""

    harness = ""

    def __init__(self, home: Path | None = None, cache: Any = None) -> None:
        self.home = Path(home) if home is not None else Path.home()
        self.cache = cache
        self.skipped: list[SkippedSource] = []
        #: Why a listing is empty or short: a missing or unreadable harness directory, said instead of nothing.
        self.notes: list[str] = []
        #: Why the last ``load_session`` returned None when the cause was a refused format.
        self.last_error = ""

    # -- ordering ---------------------------------------------------------------------------------
    @staticmethod
    def _order(headers: list[SessionHeader]) -> list[SessionHeader]:
        def epoch(h: SessionHeader) -> float:
            try:
                dt = datetime.fromisoformat((h.updated_at or "").replace("Z", "+00:00"))
            except ValueError:
                return 0.0
            return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()

        return sorted(headers, key=lambda h: (-epoch(h), h.source_session_id))

    # -- cache ------------------------------------------------------------------------------------
    def _cached(self, key: str, stamp: list[Any]) -> SessionHeader | None:
        return self.cache.get(self.harness, key, stamp) if self.cache is not None else None

    def _remember(self, key: str, stamp: list[Any], header: SessionHeader) -> None:
        if self.cache is not None:
            self.cache.put(self.harness, key, stamp, header)

    def _save_cache(self) -> None:
        if self.cache is not None:
            self.cache.save()

    # -- directory probing ------------------------------------------------------------------------
    def _walk(self, root: Path, match) -> list[Path]:
        """Files under ``root`` for which ``match(relative_parts, name)`` is true. Symlinks are never followed
        (a loop cannot hang the walk) and an absent or unreadable directory is said, not hidden."""
        try:
            present = root.exists()
        except PermissionError:
            # A parent directory we may not search: Linux raises here where other platforms say "absent".
            self.notes.append(f"{self.harness}: cannot read {root}: permission denied")
            return []
        if not present:
            self.notes.append(
                f"{self.harness}: {root} does not exist (not installed, or it keeps its files elsewhere)"
            )
            return []
        found: list[Path] = []

        def onerror(err: OSError) -> None:
            self.notes.append(f"{self.harness}: cannot read {err.filename}: {err.strerror or err}")

        for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=onerror):
            rel = Path(dirpath).relative_to(root).parts
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            for name in filenames:
                full = Path(dirpath) / name
                if match(rel, name) and not full.is_symlink():
                    found.append(full)
        return found


class _FileSessionReader(_ReaderBase):
    """Shared listing: newest ``limit`` files are converted; unreadable ones are recorded, not hidden."""

    def _files(self) -> list[Path]:  # pragma: no cover - overridden
        raise NotImplementedError

    def _convert(
        self, path: Path, light: bool = False
    ) -> SessionTranscript | None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _id_of(self, path: Path) -> str:
        """The source session id a file stands for (by name, so lookup never has to parse every file)."""
        return path.stem

    def _check_size(self, path: Path) -> None:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise SessionFormatError(
                f"file is larger than the {MAX_FILE_BYTES // (1024 * 1024)} MB import cap"
            )

    def list_sessions(self, limit: int = 50) -> list[SessionHeader]:
        self.skipped, self.notes = [], []
        files = []
        for p in self._files():
            try:
                st = p.stat()
            except OSError as exc:
                self.skipped.append(
                    SkippedSource(source=str(p), reason=f"unreadable: {exc.strerror or exc}")
                )
                continue
            files.append((st.st_mtime, st, p))
        files.sort(key=lambda t: (-t[0], str(t[2])))
        headers: list[SessionHeader] = []
        for _, st, path in files[: max(limit, 0)]:
            stamp = [st.st_mtime_ns, st.st_size]
            hit = self._cached(str(path), stamp)
            if hit is not None:
                headers.append(hit)
                continue
            try:
                self._check_size(path)
                transcript = self._convert(path, light=True)
            except SessionFormatError as exc:
                self.skipped.append(
                    SkippedSource(source=str(path), reason=f"unsupported format: {exc}")
                )
                continue
            except (OSError, ValueError, UnicodeError) as exc:
                self.skipped.append(SkippedSource(source=str(path), reason=f"unreadable: {exc}"))
                continue
            except Exception as exc:  # noqa: BLE001 - one malformed file must not take the listing down
                self.skipped.append(
                    SkippedSource(source=str(path), reason=f"unreadable: {type(exc).__name__}")
                )
                continue
            if transcript is None or not transcript.messages:
                bad = (transcript.lossy.get("invalid_lines", 0) if transcript else 0) or (
                    transcript.lossy.get("oversize_lines_skipped", 0) if transcript else 0
                )
                reason = (
                    f"no readable conversation ({bad} lines were not valid)"
                    if bad
                    else ("empty file" if st.st_size == 0 else "no conversation messages")
                )
                self.skipped.append(SkippedSource(source=str(path), reason=reason))
                continue
            self._remember(str(path), stamp, transcript.header)
            headers.append(transcript.header)
        self._save_cache()
        return self._order(headers)

    def _find(self, source_session_id: str) -> Path | None:
        if not _SAFE_ID.match(source_session_id or ""):
            return None
        for p in self._files():
            if self._id_of(p) == source_session_id:
                return p
        return None

    def load_session(self, source_session_id: str) -> SessionTranscript | None:
        self.last_error = ""
        path = self._find(source_session_id)
        if path is None:
            return None
        try:
            self._check_size(path)
            return self._convert(path)
        except SessionFormatError as exc:
            self.last_error = f"unsupported format: {exc}"
            return None
        except (OSError, ValueError, UnicodeError) as exc:
            self.last_error = f"unreadable: {exc}"
            return None
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"unreadable: {type(exc).__name__}"
            return None


# ---- Claude Code --------------------------------------------------------------------------------


class ClaudeCodeSessionReader(_FileSessionReader):
    harness = "claude-code"

    def _files(self) -> list[Path]:
        return sorted(
            self._walk(
                self.home / ".claude" / "projects",
                lambda rel, n: len(rel) == 1 and n.endswith(".jsonl"),
            )
        )

    def _convert(self, path: Path, light: bool = False) -> SessionTranscript | None:
        from axiom.memory.session_capture import _is_real_user_prompt

        b = _Builder(light)
        title = ""
        cwd = ""
        created = ""
        version = ""
        for rec in _jsonl(path, b):
            kind = rec.get("type")
            if kind == "ai-title":
                title = str(rec.get("aiTitle") or title)
                continue
            if kind not in ("user", "assistant") or rec.get("isSidechain") or rec.get("isMeta"):
                continue
            ts = str(rec.get("timestamp") or "")
            created = created or ts
            cwd = cwd or str(rec.get("cwd") or "")
            version = version or str(rec.get("version") or "")
            message = rec.get("message") or {}
            content = message.get("content") if isinstance(message, dict) else None
            if kind == "user":
                if _is_real_user_prompt(rec):
                    text = _text_of(content, ("text",))
                    if isinstance(content, list) and any(
                        isinstance(x, dict) and x.get("type") in ("image", "document")
                        for x in content
                    ):
                        b.bump("attachments_dropped")
                    b.user(text, ts)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            b.bump("tool_results")
                            b.assistant_piece(_tool_result_text(block.get("content")), ts)
                continue
            # assistant
            if isinstance(content, str):
                b.assistant_piece(content, ts)
                continue
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    b.assistant_piece(str(block.get("text") or ""), ts)
                elif block.get("type") == "tool_use":
                    b.bump("tool_calls")
                    b.assistant_piece(b.call(str(block.get("name") or "?"), block.get("input")), ts)
                elif block.get("type") in ("thinking", "redacted_thinking"):
                    b.bump("thinking_dropped")
        messages = b.done()
        header = SessionHeader(
            harness=self.harness,
            source_session_id=path.stem,
            title=_title_from(messages, title),
            created_at=created or _iso_mtime(path),
            updated_at=_iso_mtime(path),
            message_count=len(messages),
            path=path,
            cwd=cwd,
            format_version=version,
        )
        return SessionTranscript(header, messages, b.lossy)


# ---- Codex --------------------------------------------------------------------------------------


class CodexSessionReader(_FileSessionReader):
    harness = "codex"

    def _files(self) -> list[Path]:
        root = self.home / ".codex"
        out = self._walk(
            root / "sessions", lambda rel, n: n.startswith("rollout-") and n.endswith(".jsonl")
        ) + self._walk(
            root / "archived_sessions",
            lambda rel, n: n.startswith("rollout-") and n.endswith(".jsonl"),
        )
        return sorted(out)

    def _id_of(self, path: Path) -> str:
        stem = path.stem
        # rollout-<ts>-<uuid>: the id is the trailing uuid (five dash groups)
        parts = stem.split("-")
        return "-".join(parts[-5:]) if len(parts) >= 5 else stem

    def _find(self, source_session_id: str) -> Path | None:
        if not _SAFE_ID.match(source_session_id or ""):
            return None
        for p in self._files():
            if p.stem == source_session_id or p.stem.endswith("-" + source_session_id):
                return p
        return None

    def _convert(self, path: Path, light: bool = False) -> SessionTranscript | None:
        b = _Builder(light)
        session_id = ""
        cwd = ""
        created = ""
        version = ""
        for rec in _jsonl(path, b):
            payload = rec.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            ts = str(rec.get("timestamp") or "")
            if rec.get("type") == "session_meta":
                session_id = str(payload.get("id") or "")
                cwd = str(payload.get("cwd") or "")
                created = str(payload.get("timestamp") or ts)
                version = str(payload.get("cli_version") or "")
                continue
            if rec.get("type") != "response_item":
                continue
            ptype = payload.get("type")
            if ptype == "message":
                role = payload.get("role")
                if role not in ("user", "assistant"):
                    continue  # developer = the harness's own instructions
                text = _text_of(payload.get("content"), ("input_text", "output_text"))
                if role == "user":
                    if not text.strip():
                        continue
                    if text.lstrip().startswith(_CODEX_INJECTED):
                        b.bump("harness_context_dropped")
                        continue
                    b.user(text, ts)
                else:
                    b.assistant_piece(text, ts)
            elif ptype == "function_call":
                b.bump("tool_calls")
                b.assistant_piece(
                    b.call(str(payload.get("name") or "?"), payload.get("arguments")), ts
                )
            elif ptype == "function_call_output":
                b.bump("tool_results")
                b.assistant_piece(_tool_result_text(payload.get("output")), ts)
            elif ptype == "reasoning":
                b.bump("thinking_dropped")
        messages = b.done()
        sid = session_id or self._id_of(path)
        header = SessionHeader(
            harness=self.harness,
            source_session_id=sid,
            title=_title_from(messages),
            created_at=created or _iso_mtime(path),
            updated_at=_iso_mtime(path),
            message_count=len(messages),
            path=path,
            cwd=cwd,
            format_version=version,
        )
        return SessionTranscript(header, messages, b.lossy)


def session_readers(home: Path | None = None, cache: Any = None) -> dict[str, Any]:
    """Every harness that has a session reader, by the name the harness is known by."""
    from .sessions_files import (
        ClineSessionReader,
        ContinueSessionReader,
        CursorSessionReader,
        GeminiSessionReader,
        PiSessionReader,
    )
    from .sessions_sqlite import GooseSessionReader, HermesSessionReader, OpenCodeSessionReader

    classes = (
        ClaudeCodeSessionReader,
        CodexSessionReader,
        OpenCodeSessionReader,
        HermesSessionReader,
        GooseSessionReader,
        GeminiSessionReader,
        PiSessionReader,
        ClineSessionReader,
        ContinueSessionReader,
        CursorSessionReader,
    )
    return {c.harness: c(home, cache) for c in classes}


__all__ = [
    "ClaudeCodeSessionReader",
    "CodexSessionReader",
    "SessionFormatError",
    "SessionHeader",
    "SessionMessage",
    "SessionTranscript",
    "session_readers",
]
