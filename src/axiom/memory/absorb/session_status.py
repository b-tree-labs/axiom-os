# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What is known about each harness's conversation store, and whether a reader ships.

One record per harness: where its sessions live, the format, HOW the format was verified, and (for the ones with
no reader) exactly what is missing and what does exist. ``/renew`` builds its "no session reader for X" message
from this, so what the person is told is the same table the docs describe (see :mod:`.sessions`).

``verified_by`` is one of ``run`` (a real install was driven in a throwaway HOME and the session it wrote was
read back), ``real files on this machine``, ``docs`` (vendor documentation only) or ``unverified``. A reader
ships only for ``run`` and ``real files on this machine``. Versions are the ones checked, 2026-10-08.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HarnessStatus:
    harness: str
    path: str
    format: str
    verified_by: str
    version: str
    reader: bool
    missing: str = ""  # for harnesses without a reader: what is missing
    exists: str = ""  # for harnesses without a reader: what does exist for it


_RUN = "run"
_FILES = "real files on this machine"

HARNESS_STATUS: dict[str, HarnessStatus] = {
    s.harness: s
    for s in (
        HarnessStatus(
            "claude-code", "~/.claude/projects/<slug>/<uuid>.jsonl", "JSONL", _FILES, "", True
        ),
        HarnessStatus(
            "codex",
            "~/.codex/sessions/Y/M/D/rollout-*.jsonl, ~/.codex/archived_sessions/",
            "JSONL",
            _FILES,
            "0.161",
            True,
        ),
        HarnessStatus(
            "opencode",
            "~/.local/share/opencode/opencode.db",
            "SQLite (session/message/part)",
            _RUN,
            "1.17.8",
            True,
        ),
        HarnessStatus(
            "hermes",
            "~/.hermes/state.db",
            "SQLite (sessions/messages, schema_version 26)",
            _RUN,
            "0.20.4",
            True,
        ),
        HarnessStatus(
            "goose",
            "~/.local/share/goose/sessions/sessions.db",
            "SQLite (sessions/messages)",
            _RUN,
            "1.53.0",
            True,
        ),
        HarnessStatus(
            "gemini",
            "~/.gemini/tmp/<project>/chats/session-*.jsonl",
            "JSONL (metadata line + records)",
            _RUN,
            "0.63.0",
            True,
        ),
        HarnessStatus(
            "pi",
            "~/.pi/agent/sessions/--<cwd>--/<ts>_<uuid>.jsonl",
            "JSONL tree, format v3",
            _RUN,
            "0.73.1",
            True,
        ),
        HarnessStatus(
            "cline",
            "~/.cline/data/sessions/<id>/<id>.messages.json",
            "JSON (CLI v3)",
            _RUN,
            "3.0.70",
            True,
        ),
        HarnessStatus("continue", "~/.continue/sessions/<uuid>.json", "JSON", _RUN, "1.5.47", True),
        HarnessStatus(
            "cursor",
            "~/.cursor/projects/<slug>/agent-transcripts/<uuid>/<uuid>.jsonl",
            "JSONL",
            _FILES,
            "2026.10.01",
            True,
        ),
        HarnessStatus(
            "aider",
            "<project>/.aider.chat.history.md",
            "markdown log",
            _RUN,
            "0.86.2",
            False,
            missing=(
                "aider writes its log inside each project directory, with no central place to enumerate them, so "
                "there is nothing for /renew to list"
            ),
            exists="its CONVENTIONS.md rules file can be written to; the log format itself was verified by run",
        ),
        HarnessStatus(
            "letta",
            "server-side (Letta cloud, or a self-hosted server)",
            "remote API",
            "unverified",
            "0.34.6",
            False,
            missing=(
                "its conversations live on the Letta server and are reached by its API after a login; there is "
                "no session file on disk. Unverified: reading one back needs a Letta login (or a self-hosted "
                "server URL and key)"
            ),
            exists="its memory passages can be absorbed from a self-hosted server (`memory absorb`), not its conversations",
        ),
        HarnessStatus(
            "roo",
            "<VS Code globalStorage>/rooveterinaryinc.roo-cline/tasks/<id>/",
            "JSON task files (as documented)",
            "unverified",
            "",
            False,
            missing=(
                "the Roo Code VS Code extension is not installed here, so its task-file format could not be "
                "checked (unverified); a reader is not shipped from documentation alone"
            ),
            exists="its rules files can be written to; no conversation reader",
        ),
        HarnessStatus(
            "grok",
            "~/Library/Application Support/<app userData> (Electron; created on first launch)",
            "unknown",
            "unverified",
            "0.68.1",
            False,
            missing=(
                "the only Grok client installed here is the Grok Bot desktop app (bundle com.anysphere.sand, "
                "0.68.1, from the Cursor company's codebase), and it has never been opened on this Mac: it has no "
                "data directory, so there are no conversations to read and no format to verify. It bundles a "
                "transcript package, so open it once and sign in, then re-check. No Grok command-line client is "
                "installed (nothing in PATH, npm -g, brew, pipx or Cursor extensions). Not installed as a CLI, "
                "unverified"
            ),
            exists="nothing that reads its conversations",
        ),
        HarnessStatus(
            "windsurf",
            "app-internal storage",
            "unknown",
            "unverified",
            "",
            False,
            missing="its chat history is in app-internal storage whose format has not been checked (unverified)",
            exists="its rules files can be written to; no conversation reader",
        ),
        HarnessStatus(
            "vscode",
            "app-internal storage",
            "unknown",
            "unverified",
            "",
            False,
            missing="Copilot Chat history in VS Code is app-internal and its format has not been checked (unverified)",
            exists="nothing that reads its conversations",
        ),
        HarnessStatus(
            "claude-desktop",
            "kept by the Claude service",
            "remote",
            "unverified",
            "",
            False,
            missing="conversations are kept by the service rather than in local session files (unverified)",
            exists="nothing that reads its conversations",
        ),
    )
}

#: Said once for a harness that HAS a reader but whose chat-mode store is a different, unread file.
READER_GAPS: dict[str, str] = {
    "cursor": (
        "Cursor's agent transcripts are read; its chat-mode history (state.vscdb) and the cursor-agent CLI's "
        "~/.cursor/chats store are not (an app-internal key-value schema; and no logged-in CLI to verify)"
    ),
    "cline": "the Cline CLI (3.x) store is read; the older VS Code extension's tasks/ files are not (not installed to verify)",
    "gemini": "JSONL sessions (0.63) are read; an older single-JSON session file is reported as unsupported",
}

__all__ = ["HARNESS_STATUS", "HarnessStatus", "READER_GAPS"]
